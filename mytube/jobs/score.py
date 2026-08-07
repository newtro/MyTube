"""Scorer job: kNN-score every eligible candidate per user (FLOW-14 input).

Reuses the Phase-0 scoring module against the model of record. Phase 4
additions: active steering terms join the taste matrices as signed
pseudo-taste-items (AC-12/14), the top kNN anchors are captured per score
with durable title snapshots for receipts (AC-15/35), and per-channel
promise stats are refreshed for the rules-layer penalty (AC-37).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import numpy as np

from mytube import steering, store
from mytube.scheduler import JobContext
from mytube.scoring import TasteMatrix, normalize_rows, score_candidates

ANCHORS_PER_CANDIDATE = 3


def _anchor_payload(
    cand_vectors: np.ndarray,
    pos_matrix: TasteMatrix,
    titles: dict[str, str],
) -> list[list[dict]]:
    """Top positive neighbors per candidate, with title snapshots taken NOW
    (they must outlive API-metadata expiry, AC-35). Taste ids are video ids;
    steering pseudo-items use 'steering:<term_id>' ids and carry their term
    text as the title."""
    cand = normalize_rows(cand_vectors)
    sims = cand @ pos_matrix.vectors.T
    n = min(ANCHORS_PER_CANDIDATE, sims.shape[1])
    top_idx = np.argpartition(-sims, n - 1, axis=1)[:, :n]
    payloads: list[list[dict]] = []
    for row_i in range(sims.shape[0]):
        # only genuinely-similar neighbors: a zero/negative-cosine "anchor"
        # would be a misleading "because you watched X"
        idx = [
            j for j in sorted(top_idx[row_i], key=lambda j: -sims[row_i, j])
            if sims[row_i, j] > 0.05
        ]
        anchors = []
        for j in idx:
            anchor_id = pos_matrix.video_ids[j]
            if anchor_id.startswith("steering:"):
                anchors.append({
                    "kind": "steering",
                    "id": anchor_id,
                    "title": titles.get(anchor_id, ""),
                    "sim": round(float(sims[row_i, j]), 4),
                })
            else:
                anchors.append({
                    "kind": "taste",
                    "id": anchor_id,
                    "title": titles.get(anchor_id, ""),
                    "sim": round(float(sims[row_i, j]), 4),
                })
        payloads.append(anchors)
    return payloads


def refresh_promise_stats(conn, settings, user_id: int) -> int:
    """DEC-12 v1: overpromise per (user, channel) = clickbait-rate share +
    completion deficit vs the user's own baseline. Neutral (0) until >= 5
    observed watches (AC-37). Computed from durable-ish inputs: ended watch
    sessions (completion) + feedback events."""
    cur = conn.execute(
        """
        WITH completions AS (
            -- completion = actually-watched seconds (merged ranges) over
            -- duration; exposure gate mirrors the DEC-9 negative threshold
            SELECT v.channel_id,
                   LEAST((
                       SELECT COALESCE(sum((r->>1)::real - (r->>0)::real), 0)
                       FROM jsonb_array_elements(ws.watched_ranges) r
                   ) / v.duration_seconds, 1.0) AS completion
            FROM watch_sessions ws
            JOIN videos v ON v.video_id = ws.video_id
            WHERE ws.user_id = %(uid)s
              AND v.channel_id IS NOT NULL
              AND v.duration_seconds IS NOT NULL AND v.duration_seconds > 0
              AND ws.exposure_seconds >= 30
              -- finalized observations only: a session mid-watch would count
              -- its partial ranges as low completion. Sessions that never got
              -- a clean close beacon settle after 6h of silence.
              AND (ws.ended_at IS NOT NULL
                   OR ws.updated_at < now() - interval '6 hours')
        ),
        sessions AS (
            SELECT channel_id, count(*) AS watches,
                   sum(completion) AS sum_completion,
                   avg(completion) AS avg_completion
            FROM completions GROUP BY channel_id
        ),
        totals AS (
            SELECT count(*) AS n, sum(completion) AS s FROM completions
        ),
        clickbait AS (
            -- DISTINCT videos: repeated flags on one video are one signal
            SELECT v.channel_id, count(DISTINCT f.video_id) AS n
            FROM feedback_events f
            JOIN videos v ON v.video_id = f.video_id
            WHERE f.user_id = %(uid)s AND f.kind = 'clickbait'
              AND v.channel_id IS NOT NULL
            GROUP BY v.channel_id
        )
        INSERT INTO channel_promise_stats
            (user_id, channel_id, watches, avg_completion,
             baseline_completion, clickbait_count, overpromise, computed_at)
        SELECT %(uid)s, s.channel_id, s.watches, s.avg_completion,
               -- leave-one-channel-out baseline: a channel must not dampen
               -- its own deficit (a sole channel gets NULL -> no deficit)
               (t.s - s.sum_completion) / NULLIF(t.n - s.watches, 0),
               COALESCE(c.n, 0),
               CASE WHEN s.watches >= %(min_watches)s THEN
                   LEAST(
                       %(fb_w)s * (COALESCE(c.n, 0)::real / s.watches)
                       + %(def_w)s * GREATEST(COALESCE(
                             (t.s - s.sum_completion)
                             / NULLIF(t.n - s.watches, 0)
                             - s.avg_completion, 0), 0),
                       1.0)
               ELSE 0 END,
               now()
        FROM sessions s CROSS JOIN totals t
        LEFT JOIN clickbait c ON c.channel_id = s.channel_id
        ON CONFLICT (user_id, channel_id) DO UPDATE SET
            watches = EXCLUDED.watches,
            avg_completion = EXCLUDED.avg_completion,
            baseline_completion = EXCLUDED.baseline_completion,
            clickbait_count = EXCLUDED.clickbait_count,
            overpromise = EXCLUDED.overpromise,
            computed_at = now()
        """,
        {
            "uid": user_id,
            "min_watches": settings.promise_min_watches,
            "fb_w": settings.promise_feedback_weight,
            "def_w": settings.promise_deficit_weight,
        },
    )
    return cur.rowcount


def score_users(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    model_id = settings.model_of_record()
    # a brand-new user with ONLY a steering directive still gets scores —
    # positive pseudo-terms can bootstrap recommendations before any watch.
    # Users with EXISTING scores are always revisited: when their last
    # positive basis dies (directive expired, no taste), the stale scores
    # must be wiped, not served forever (AC-14).
    users = conn.execute(
        """
        SELECT DISTINCT user_id FROM taste_items
        UNION
        SELECT DISTINCT t.user_id
        FROM steering_terms t
        JOIN steering_directives d ON d.directive_id = t.directive_id
        WHERE d.expires_at > now() AND d.cancelled_at IS NULL
        UNION
        SELECT DISTINCT user_id FROM video_scores
        """
    ).fetchall()
    scored_total = 0
    now = datetime.now(UTC)
    for row in users:
        user_id = row["user_id"]
        ctx.heartbeat()
        refresh_promise_stats(conn, settings, user_id)
        taste = store.taste_items(conn, user_id)
        positives = [t for t in taste if t["polarity"] == 1]
        negatives = [t for t in taste if t["polarity"] == -1]
        # ---- steering pseudo-taste-items (AC-12; only live ones, AC-14) ----
        terms = [
            t for t in steering.active_terms(conn, user_id)
            if t["model_id"] == model_id
        ]
        pos_terms = [t for t in terms if t["polarity"] == 1]
        neg_terms = [t for t in terms if t["polarity"] == -1]
        # score exactly what the composer could serve (full Phase-3 reach:
        # subscriptions + expanded channels + search + trending)
        from mytube import composer

        candidates = composer.eligible_pool(conn, settings, user_id)
        if not positives and not pos_terms:
            # no positive basis left (taste-less user whose directives all
            # died): stale steering-era scores must not keep composing
            conn.execute(
                "DELETE FROM video_scores WHERE user_id = %s", (user_id,)
            )
            conn.commit()
            continue
        if not candidates:
            # every previously-scored video has left the pool
            conn.execute(
                "DELETE FROM video_scores WHERE user_id = %s", (user_id,)
            )
            conn.commit()
            continue
        # cleanup runs on the PRE-embedding pool, before any early exits:
        # departed-pool (watched/blocked/tombstoned) and stale-model rows
        # must disappear even while every remaining candidate awaits its
        # embedding (the later gates would otherwise skip this for the
        # whole backlog window)
        conn.execute(
            """
            DELETE FROM video_scores
            WHERE user_id = %s
              AND (model_id != %s OR video_id != ALL(%s))
            """,
            (user_id, model_id, [c["video_id"] for c in candidates]),
        )
        conn.commit()
        all_ids = [t["video_id"] for t in taste] + [c["video_id"] for c in candidates]
        embeddings = store.load_embeddings(conn, model_id, all_ids)
        pos = [t for t in positives if t["video_id"] in embeddings]
        neg = [t for t in negatives if t["video_id"] in embeddings]
        cands = [c for c in candidates if c["video_id"] in embeddings]
        if (not pos and not pos_terms) or not cands:
            continue

        def build_matrix(items, term_rows, vecs=embeddings):
            ids = [t["video_id"] for t in items] + [
                f"steering:{t['term_id']}" for t in term_rows
            ]
            vectors = [vecs[t["video_id"]] for t in items] + [
                t["embedding"] for t in term_rows
            ]
            weights = [t["weight"] for t in items] + [
                t["weight"] * settings.steering_weight for t in term_rows
            ]
            stamps = [t["last_event_at"] for t in items] + [
                t["created_at"] for t in term_rows
            ]
            return TasteMatrix.build(
                ids, np.stack(vectors), weights, stamps,
                as_of=now, half_life_days=settings.taste_half_life_days,
            )

        pos_matrix = build_matrix(pos, pos_terms)
        neg_matrix = None
        if neg or neg_terms:
            neg_matrix = build_matrix(neg, neg_terms)
        matrix = np.stack([embeddings[c["video_id"]] for c in cands])
        scores = score_candidates(
            matrix, pos_matrix, neg_matrix,
            k=settings.knn_k,
            k_negative=settings.knn_negative_k,
            negative_penalty_weight=settings.negative_penalty_weight,
        )
        # ---- receipts: durable anchor snapshots (AC-15/35) ----
        titles = _anchor_titles(conn, user_id, pos, pos_terms)
        anchors = _anchor_payload(matrix, pos_matrix, titles)
        for c, s, a in zip(cands, scores, anchors, strict=True):
            conn.execute(
                """
                INSERT INTO video_scores (user_id, video_id, model_id, score,
                                          anchors)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, video_id) DO UPDATE SET
                    model_id = EXCLUDED.model_id, score = EXCLUDED.score,
                    anchors = EXCLUDED.anchors, scored_at = now()
                """,
                (user_id, c["video_id"], model_id, float(s), json.dumps(a)),
            )
        conn.commit()
        scored_total += len(cands)
    return {"users": len(users), "scored": scored_total, "model": model_id}


def _anchor_titles(conn, user_id: int, pos: list[dict], pos_terms) -> dict:
    """Title snapshots for anchor items: live video title first, durable
    watch-event snapshot as fallback (survives metadata expiry)."""
    ids = [t["video_id"] for t in pos]
    rows = conn.execute(
        """
        SELECT v.video_id,
               COALESCE(v.title, (
                   SELECT w.title_snapshot FROM watch_events w
                   WHERE w.user_id = %s AND w.video_id = v.video_id
                     AND w.title_snapshot IS NOT NULL
                   ORDER BY w.watched_at DESC LIMIT 1
               )) AS title
        FROM videos v WHERE v.video_id = ANY(%s)
        """,
        (user_id, ids),
    ).fetchall()
    titles = {r["video_id"]: r["title"] or "" for r in rows}
    for t in pos_terms:
        titles[f"steering:{t['term_id']}"] = t["term_text"]
    return titles
