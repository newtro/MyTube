"""Edition composer (FLOW-14; DEC-16/21/22/23; AC-1..4, AC-28, AC-39, AC-41).

Two-stage assignment:
  Stage 1 — exploration: seeded sequential uniform draws (without
  replacement) from the exploration-eligible set. Every draw logs its EXACT
  conditional selection probability (1/|remaining|), the seed, the draw
  index, and a canonical eligible snapshot, so a seeded fixture reproduces
  the logged propensities exactly (AC-39). 1-2 picks carry the orthogonal
  ``blind`` flag (DEC-21).
  Stage 2 — deterministic quota fill by adjusted score (score minus
  carry-over staleness penalty, DEC-23); propensity 1.0.

Hard filters (AC-3): Shorts by duration, blocked channels, tombstones,
not-interested, already watched/liked. Fatigued cards (>= N unclicked
impressions) are stage-2-ineligible but stage-1-eligible (AC-4).

Phase 2 scope: subscription provenance only; discovery/wildcard quotas
activate in Phase 3 and borrow into subscription until then (AC-2).

Composition is serialized per (user, local_date) with a transaction-scoped
advisory lock; exactly one automatic baseline per local date (DST-safe via
the date-keyed unique index); a failed compose rolls back wholesale so the
previous ready edition keeps serving (AC-41).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import secrets
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import psycopg

from mytube.config import Settings


class ComposeError(Exception):
    pass


def get_user_settings(conn: psycopg.Connection, user_id: int) -> dict:
    # race-safe default creation (scheduler + request can arrive together)
    conn.execute(
        "INSERT INTO user_settings (user_id) VALUES (%s) "
        "ON CONFLICT (user_id) DO NOTHING",
        (user_id,),
    )
    return conn.execute(
        "SELECT * FROM user_settings WHERE user_id = %s", (user_id,)
    ).fetchone()


def local_date_for(user_settings: dict, now: datetime | None = None):
    now = now or datetime.now(UTC)
    return now.astimezone(ZoneInfo(user_settings["timezone"])).date()


def eligible_pool(
    conn: psycopg.Connection, settings: Settings, user_id: int
) -> list[dict]:
    """Candidates surviving every hard filter, with score / carry-over /
    fatigue annotations. Canonically ordered by video_id."""
    return conn.execute(
        """
        SELECT v.video_id, v.title, v.channel_id, v.duration_seconds,
               v.published_at,
               s.score,
               fs.first_selected_at,
               COALESCE(cps.overpromise, 0) AS overpromise,
               COALESCE(imp.unclicked, 0) AS unclicked_impressions,
               EXISTS (SELECT 1 FROM watch_later q
                       WHERE q.user_id = %(uid)s
                         AND q.video_id = v.video_id) AS in_queue,
               -- provenance class (§8): subscription beats discovery beats
               -- wildcard; expanded-channel RSS uploads are discovery. A
               -- trending-first video THIS user's search also returned is
               -- discovery (precedence discovery > wildcard), so the
               -- sightings check comes before the trending branch
               CASE
                 WHEN EXISTS (SELECT 1 FROM subscriptions sub2
                              WHERE sub2.user_id = %(uid)s
                                AND sub2.channel_id = v.channel_id)
                   THEN 'subscription'
                 WHEN EXISTS (SELECT 1 FROM search_sightings ss2
                              WHERE ss2.video_id = v.video_id
                                AND ss2.user_id = %(uid)s)
                   THEN 'discovery'
                 WHEN v.first_source = 'trending' THEN 'wildcard'
                 ELSE 'discovery'
               END AS provenance
        FROM videos v
        -- model-of-record filter: after a model swap, stale-model scores
        -- must not keep composing — the video ranks as unscored until the
        -- new model has re-scored it (fail-conservative)
        LEFT JOIN video_scores s
               ON s.user_id = %(uid)s AND s.video_id = v.video_id
              AND s.model_id = %(model)s
        LEFT JOIN channel_promise_stats cps
               ON cps.user_id = %(uid)s AND cps.channel_id = v.channel_id
        LEFT JOIN LATERAL (
            SELECT min(sel.created_at) AS first_selected_at
            FROM selections sel
            WHERE sel.user_id = %(uid)s AND sel.video_id = v.video_id
        ) fs ON TRUE
        LEFT JOIN LATERAL (
            SELECT count(*) AS unclicked
            FROM impressions i
            JOIN selections sel2 ON sel2.selection_id = i.selection_id
            WHERE sel2.user_id = %(uid)s AND sel2.video_id = v.video_id
              AND NOT EXISTS (
                  SELECT 1 FROM interactions x
                  WHERE x.selection_id = sel2.selection_id AND x.kind = 'play'
              )
        ) imp ON TRUE
        WHERE v.metadata_state = 'fresh'
          AND v.duration_seconds > %(shorts)s
          AND v.tombstoned_at IS NULL
          -- acquisition reach (DEC-7): subscribed-channel uploads,
          -- expanded-channel uploads (FLOW-24), search results (FLOW-13),
          -- and trending (FLOW-25); likes/takeout stay taste-only
          AND COALESCE(v.first_source, '') NOT IN ('takeout', 'likes')
          AND (
              EXISTS (SELECT 1 FROM subscriptions sub
                      WHERE sub.user_id = %(uid)s
                        AND sub.channel_id = v.channel_id)
              OR EXISTS (SELECT 1 FROM channels cx
                         WHERE cx.channel_id = v.channel_id AND cx.expanded)
              -- search reach is per-user via sightings: every result THIS
              -- user's searches returned (another user's search must not
              -- widen my pool; my own re-find of a known video must)
              OR EXISTS (SELECT 1 FROM search_sightings ss
                         WHERE ss.video_id = v.video_id
                           AND ss.user_id = %(uid)s)
              OR v.first_source = 'trending'
          )
          AND NOT EXISTS (SELECT 1 FROM user_channel_prefs p
                          WHERE p.user_id = %(uid)s AND p.channel_id = v.channel_id
                            AND p.blocked)
          AND NOT EXISTS (SELECT 1 FROM watch_events w
                          WHERE w.user_id = %(uid)s AND w.video_id = v.video_id)
          AND NOT EXISTS (SELECT 1 FROM like_events l
                          WHERE l.user_id = %(uid)s AND l.video_id = v.video_id)
          AND NOT EXISTS (SELECT 1 FROM feedback_events f
                          WHERE f.user_id = %(uid)s AND f.video_id = v.video_id
                            AND f.kind = 'not_interested')
        ORDER BY v.video_id
        """,
        {"uid": user_id, "shorts": settings.shorts_max_seconds,
         "model": settings.model_of_record()},
    ).fetchall()


def _hash_ids(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def channel_half_lives(
    conn: psycopg.Connection, settings: Settings
) -> dict[str, float]:
    """Per-channel freshness half-life derived from upload cadence.

    A channel's median gap between consecutive uploads says how fast its
    content is superseded: post daily and a week-old video sits behind
    seven newer ones; post monthly and it is still the latest. Shorts are
    excluded — they follow a different cadence and are not candidates
    anyway. Channels with fewer than ``freshness_cadence_min_samples`` gaps
    are omitted, and the caller falls back to the fixed half-life.
    """
    rows = conn.execute(
        """
        WITH gaps AS (
            SELECT channel_id,
                   EXTRACT(EPOCH FROM (
                       published_at - lag(published_at) OVER (
                           PARTITION BY channel_id ORDER BY published_at)
                   )) / 86400.0 AS gap_days
            FROM videos
            WHERE channel_id IS NOT NULL AND published_at IS NOT NULL
              AND NOT COALESCE(is_short, FALSE)
        )
        SELECT channel_id,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY gap_days) AS median_gap
        FROM gaps
        WHERE gap_days IS NOT NULL AND gap_days > 0
        GROUP BY channel_id
        HAVING count(*) >= %(min_samples)s
        """,
        {"min_samples": settings.freshness_cadence_min_samples},
    ).fetchall()
    out: dict[str, float] = {}
    for r in rows:
        if not r["median_gap"]:
            continue
        out[r["channel_id"]] = min(
            max(
                float(r["median_gap"]) * settings.freshness_cadence_multiplier,
                settings.freshness_half_life_min_days,
            ),
            settings.freshness_half_life_max_days,
        )
    return out


def _half_life_for(
    candidate: dict,
    user_settings: dict,
    settings: Settings,
    half_lives: dict[str, float] | None,
) -> float:
    """The candidate's effective half-life: its channel's cadence-derived
    value when adaptive mode is on and we have enough history, otherwise
    the user's fixed setting (then the server default)."""
    fixed = (
        user_settings.get("freshness_half_life_days")
        or settings.freshness_half_life_days
    )
    if not user_settings.get("freshness_adaptive", True) or not half_lives:
        return fixed
    return half_lives.get(candidate.get("channel_id"), fixed)


def _freshness_penalty(
    published_at, now: datetime, half_life_days: float, weight: float
) -> float:
    """§6 rules layer: demote by PUBLISH age on a bounded exponential.

    penalty = weight * (1 - 0.5 ** (age_days / half_life))

    At age 0 it is 0; at the half-life it is half of ``weight``; it
    asymptotes to ``weight`` and never exceeds it, so an old video is
    pushed down but a genuinely strong match is never inverted into
    negative territory. Unknown publish dates are NOT penalized (we don't
    punish missing metadata), and future timestamps clamp to 0.
    """
    if not weight or published_at is None or half_life_days <= 0:
        return 0.0
    age_days = (now - published_at).total_seconds() / 86400.0
    if age_days <= 0:
        return 0.0
    return weight * (1.0 - 0.5 ** (age_days / half_life_days))


def _duration_fit_penalty(
    duration_seconds, typical_duration, weight: float
) -> float:
    """§6 rules layer: demote videos far from the user's typical watched
    length — |log2(duration/typical)| capped at 3 octaves. Symmetric (a 4x
    shorter video is as off-fit as a 4x longer one); no signal, no penalty."""
    if not weight or not typical_duration or not duration_seconds:
        return 0.0
    octaves = abs(math.log2(duration_seconds / typical_duration))
    return weight * min(octaves, 3.0)


def typical_watched_duration(
    conn: psycopg.Connection, user_id: int
) -> float | None:
    """Median duration of the user's watched videos (durable facts)."""
    row = conn.execute(
        """
        SELECT percentile_cont(0.5) WITHIN GROUP
                   (ORDER BY v.duration_seconds) AS typical
        FROM (SELECT DISTINCT video_id FROM watch_events
              WHERE user_id = %s) w
        JOIN videos v ON v.video_id = w.video_id
        WHERE v.duration_seconds IS NOT NULL AND v.duration_seconds > 0
        """,
        (user_id,),
    ).fetchone()
    return float(row["typical"]) if row and row["typical"] else None


def plan_edition(
    pool: list[dict],
    settings: Settings,
    user_settings: dict,
    *,
    rng_seed: int,
    now: datetime | None = None,
    typical_duration: float | None = None,
    half_lives: dict[str, float] | None = None,
) -> dict:
    """Pure planning: pool -> ordered selection dicts. DB-free and fully
    deterministic given (pool, settings, seed) — the AC-39 test surface.
    ``typical_duration`` (median of the user's watched durations, seconds)
    drives the §6 duration-fit adjustment; None disables it."""
    now = now or datetime.now(UTC)
    size = user_settings["edition_size"]
    k_exploration = min(user_settings["exploration_slots"], size)
    fatigue_n = settings.fatigue_impressions
    rng = random.Random(rng_seed)

    scored = sorted(
        c["score"] for c in pool if c["score"] is not None
    )
    median = scored[len(scored) // 2] if scored else None

    # stage 1 eligibility: unscored, below-median score, or fatigued (AC-4),
    # and not ancient. The age ceiling bounds the ELIGIBLE SET; the draw
    # over it stays uniform, so every logged propensity is still exactly
    # 1/|remaining| against the snapshot recorded here (AC-39).
    max_age = settings.exploration_max_age_days

    def _recent_enough(c: dict) -> bool:
        if not max_age:
            return True
        published = c.get("published_at")
        if published is None:
            return True                      # unknown date isn't a strike
        return (now - published).total_seconds() / 86400.0 <= max_age

    explore_eligible = [
        c for c in pool
        if _recent_enough(c)
        and (
            c["score"] is None
            or (median is not None and c["score"] <= median)
            or c["unclicked_impressions"] >= fatigue_n
        )
    ]
    explore_ids = [c["video_id"] for c in explore_eligible]
    explore_hash = _hash_ids(explore_ids)

    picks: list[dict] = []
    remaining = list(explore_eligible)          # canonical (video_id) order
    for draw_index in range(min(k_exploration, len(remaining))):
        n = len(remaining)
        idx = rng.randrange(n)
        chosen = remaining.pop(idx)
        picks.append(
            {
                "video_id": chosen["video_id"],
                "slot_class": "exploration",
                "sampling_stratum": "uniform_exploration",
                "propensity": 1.0 / n,          # exact conditional probability
                "rng_seed": rng_seed,
                "draw_index": draw_index,
                "eligible_set_hash": explore_hash,
                "eligible_snapshot": explore_ids,
                "score": chosen["score"],
                "blind": False,
            }
        )

    # blind flags (DEC-21): 1-2 of the exploration picks, seeded. Videos the
    # user already queued are excluded — their packaging is already known,
    # so the blind outcome would be worthless training data.
    queued_ids = {c["video_id"] for c in pool if c.get("in_queue")}
    blind_candidates = [
        i for i, p in enumerate(picks) if p["video_id"] not in queued_ids
    ]
    if blind_candidates:
        blind_count = min(
            rng.randint(settings.blind_min, settings.blind_max),
            len(blind_candidates),
        )
        for i in rng.sample(blind_candidates, blind_count):
            picks[i]["blind"] = True

    # stage 2: deterministic top-K by adjusted score (staleness decay DEC-23
    # + overpromise rules-layer penalty DEC-12/AC-37 + duration-fit §6)
    taken = {p["video_id"] for p in picks}
    penalty = user_settings["staleness_penalty_per_day"]

    def adjusted(c: dict) -> float:
        base = c["score"]
        if c["first_selected_at"] is not None:
            days = max((now - c["first_selected_at"]).total_seconds() / 86400.0, 0.0)
            base -= penalty * days
        base -= settings.overpromise_penalty_weight * (c.get("overpromise") or 0)
        base -= _duration_fit_penalty(
            c.get("duration_seconds"),
            typical_duration,
            user_settings.get("duration_fit_weight") or 0,
        )
        base -= _freshness_penalty(
            c.get("published_at"), now,
            _half_life_for(c, user_settings, settings, half_lives),
            user_settings.get("freshness_weight")
            if user_settings.get("freshness_weight") is not None
            else settings.freshness_weight,
        )
        return base

    stage2_eligible = [
        c for c in pool
        if c["video_id"] not in taken
        and c["score"] is not None
        and c["unclicked_impressions"] < fatigue_n
    ]
    stage2_ids = [c["video_id"] for c in stage2_eligible]
    stage2_hash = _hash_ids(stage2_ids)
    quota_capacity = (
        user_settings["subscription_quota"]
        + user_settings["discovery_quota"]
        + user_settings["wildcard_quota"]
    )
    slots = min(size - len(picks), quota_capacity)

    # Stage 2 (§8/AC-1): fill by provenance precedence subscription >
    # discovery > wildcard into the configured quotas, then borrow leftover
    # slots across types — EXCEPT trending/wildcard candidates, which are
    # capped at the wildcard quota (AC-31: trending never enters the
    # subscription/discovery fill).
    by_class: dict[str, list[dict]] = {"subscription": [], "discovery": [],
                                       "wildcard": []}
    for c in stage2_eligible:
        by_class[c.get("provenance") or "subscription"].append(c)
    for members in by_class.values():
        members.sort(key=lambda c: (-adjusted(c), c["video_id"]))

    quotas = [
        ("subscription", user_settings["subscription_quota"]),
        ("discovery", user_settings["discovery_quota"]),
        ("wildcard", user_settings["wildcard_quota"]),
    ]
    stage2_picks: list[dict] = []
    for cls, class_quota in quotas:
        take = min(class_quota, len(by_class[cls]), slots - len(stage2_picks))
        stage2_picks.extend(
            {"candidate": c, "slot_class": cls} for c in by_class[cls][:take]
        )
        by_class[cls] = by_class[cls][take:]
    # borrowing (AC-2): leftover subscription+discovery candidates compete
    # for any remaining slots by adjusted score; slot_class keeps the
    # candidate's own provenance
    if len(stage2_picks) < slots:
        leftovers = sorted(
            by_class["subscription"] + by_class["discovery"],
            key=lambda c: (-adjusted(c), c["video_id"]),
        )
        for c in leftovers[: slots - len(stage2_picks)]:
            stage2_picks.append(
                {"candidate": c,
                 "slot_class": c.get("provenance") or "subscription"}
            )

    for entry in stage2_picks:
        c = entry["candidate"]
        picks.append(
            {
                "video_id": c["video_id"],
                "slot_class": entry["slot_class"],
                "sampling_stratum": "deterministic_topk",
                "propensity": 1.0,
                "rng_seed": None,
                "draw_index": None,
                "eligible_set_hash": stage2_hash,
                "eligible_snapshot": stage2_ids,
                "score": c["score"],
                "blind": False,
            }
        )

    note = None
    if len(picks) < size:
        if not stage2_eligible and not scored:
            reason = "no scored candidates yet (the scorer hasn't run)"
        elif len(stage2_picks) < slots:
            reason = "candidate pool exhausted after hard filters"
        else:
            reason = "slot quotas below edition size"
        note = f"short edition: only {len(picks)} of {size} cards — {reason} (AC-2)"
    return {"picks": picks, "note": note, "scored_available": len(scored)}


def compose_edition(
    conn: psycopg.Connection,
    settings: Settings,
    user_id: int,
    *,
    automatic: bool,
    now: datetime | None = None,
) -> dict:
    """Compose one edition inside a per-(user, local_date) advisory lock.

    Everything happens in the caller's transaction: a crash/failure rolls
    back wholesale and the previous ready edition keeps serving (AC-41).
    The caller commits.
    """
    user_settings = get_user_settings(conn, user_id)
    local_date = local_date_for(user_settings, now)

    lock_key = int.from_bytes(
        hashlib.sha256(f"compose:{user_id}:{local_date}".encode()).digest()[:8],
        "big", signed=True,
    )
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))

    if automatic:
        existing = conn.execute(
            """
            SELECT edition_id FROM editions
            WHERE user_id = %s AND local_date = %s AND is_automatic
            """,
            (user_id, local_date),
        ).fetchone()
        if existing:
            return {"skipped": "baseline-exists", "local_date": str(local_date)}

    generation = conn.execute(
        """
        SELECT COALESCE(max(generation_no), -1) + 1 AS g FROM editions
        WHERE user_id = %s AND local_date = %s
        """,
        (user_id, local_date),
    ).fetchone()["g"]

    pool = eligible_pool(conn, settings, user_id)
    if not pool:
        raise ComposeError("candidate pool is empty — nothing to compose")

    rng_seed = secrets.randbits(63)
    plan = plan_edition(
        pool, settings, user_settings, rng_seed=rng_seed, now=now,
        typical_duration=typical_watched_duration(conn, user_id),
        half_lives=channel_half_lives(conn, settings),
    )
    if not plan["picks"]:
        raise ComposeError("no eligible cards after planning")
    if automatic and plan["scored_available"] == 0:
        # don't burn the day's single automatic baseline on a tiny
        # exploration-only edition just because the scorer hasn't run yet;
        # a manual regenerate can still force one
        raise ComposeError(
            "no scored candidates yet — waiting for the scorer before "
            "composing the daily baseline"
        )

    edition = conn.execute(
        """
        INSERT INTO editions
            (user_id, local_date, generation_no, is_automatic, state,
             policy_version, rng_seed, note)
        VALUES (%s, %s, %s, %s, 'composing', %s, %s, %s)
        RETURNING edition_id
        """,
        (user_id, local_date, generation, automatic,
         settings.policy_version, rng_seed, plan["note"]),
    ).fetchone()
    for position, pick in enumerate(plan["picks"]):
        conn.execute(
            """
            INSERT INTO selections
                (edition_id, user_id, video_id, position, slot_class, blind,
                 policy_version, sampling_stratum, propensity, rng_seed,
                 draw_index, eligible_set_hash, eligible_snapshot, score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                edition["edition_id"], user_id, pick["video_id"], position,
                pick["slot_class"], pick["blind"], settings.policy_version,
                pick["sampling_stratum"], pick["propensity"], pick["rng_seed"],
                pick["draw_index"], pick["eligible_set_hash"],
                json.dumps(pick["eligible_snapshot"]), pick["score"],
            ),
        )
    # Final revalidation inside the transaction: a block/not-interested/
    # tombstone/Short-reclassification/watch/like committed between the pool
    # read and here must not ship in a ready edition (AC-3/AC-8/AC-9/AC-18)
    violations = conn.execute(
        """
        SELECT count(*) AS n FROM selections s
        JOIN videos v ON v.video_id = s.video_id
        WHERE s.edition_id = %(eid)s AND (
            v.tombstoned_at IS NOT NULL
            OR v.metadata_state != 'fresh'
            OR v.duration_seconds IS NULL
            OR v.duration_seconds <= %(shorts)s
            OR EXISTS (SELECT 1 FROM user_channel_prefs p
                       WHERE p.user_id = %(uid)s
                         AND p.channel_id = v.channel_id AND p.blocked)
            OR EXISTS (SELECT 1 FROM feedback_events f
                       WHERE f.user_id = %(uid)s AND f.video_id = s.video_id
                         AND f.kind = 'not_interested')
            OR EXISTS (SELECT 1 FROM watch_events w
                       WHERE w.user_id = %(uid)s AND w.video_id = s.video_id)
            OR EXISTS (SELECT 1 FROM like_events l
                       WHERE l.user_id = %(uid)s AND l.video_id = s.video_id)
            -- acquisition reach must still hold (e.g. unsubscribe
            -- mid-compose); COALESCE: NULL first_source must count as a
            -- violation, not as SQL-unknown
            OR NOT COALESCE((
                EXISTS (SELECT 1 FROM subscriptions sub
                        WHERE sub.user_id = %(uid)s
                          AND sub.channel_id = v.channel_id)
                OR EXISTS (SELECT 1 FROM channels cx
                           WHERE cx.channel_id = v.channel_id AND cx.expanded)
                OR EXISTS (SELECT 1 FROM search_sightings ss
                           WHERE ss.video_id = v.video_id
                             AND ss.user_id = %(uid)s)
                OR v.first_source = 'trending'
            ), FALSE)
            -- a card planned into a subscription slot must STILL be
            -- subscription-reachable: an unsubscribe mid-compose must not
            -- let a trending-first video ship in a subscription slot (AC-31)
            OR (s.slot_class = 'subscription'
                AND NOT EXISTS (SELECT 1 FROM subscriptions sub3
                                WHERE sub3.user_id = %(uid)s
                                  AND sub3.channel_id = v.channel_id))
        )
        """,
        {"eid": edition["edition_id"], "uid": user_id,
         "shorts": settings.shorts_max_seconds},
    ).fetchone()["n"]
    if violations:
        raise ComposeError(
            f"{violations} picks became ineligible mid-compose — retrying"
        )
    conn.execute(
        """
        UPDATE editions SET state = 'archived'
        WHERE user_id = %s AND state = 'ready' AND edition_id != %s
        """,
        (user_id, edition["edition_id"]),
    )
    conn.execute(
        "UPDATE editions SET state = 'ready', ready_at = now() WHERE edition_id = %s",
        (edition["edition_id"],),
    )
    return {
        "edition_id": edition["edition_id"],
        "local_date": str(local_date),
        "generation_no": generation,
        "cards": len(plan["picks"]),
        "note": plan["note"],
    }


def serving_edition(conn: psycopg.Connection, user_id: int) -> dict | None:
    """The latest ready edition (AC-41: serving always uses latest ready)."""
    return conn.execute(
        """
        SELECT * FROM editions
        WHERE user_id = %s AND state = 'ready'
        ORDER BY local_date DESC, generation_no DESC
        LIMIT 1
        """,
        (user_id,),
    ).fetchone()


def replace_selection(
    conn: psycopg.Connection,
    settings: Settings,
    user_id: int,
    selection_id: int,
    *,
    confirmed_gone: bool,
) -> dict | None:
    """FLOW-20/AC-38: swap a dead card for the best not-yet-selected
    stage-2 candidate. The GLOBAL tombstone happens only when the caller
    verified upstream that the video is truly gone (§5: 'unavailable'
    is reserved for confirmed deleted/private — one user's client-side
    error report must not nuke a shared video)."""
    sel = conn.execute(
        """
        SELECT s.selection_id, s.edition_id, s.video_id, s.position,
               s.replaced_at, s.slot_class
        FROM selections s
        JOIN editions e ON e.edition_id = s.edition_id
        WHERE s.selection_id = %s AND s.user_id = %s AND e.state = 'ready'
        FOR UPDATE OF s
        """,
        (selection_id, user_id),
    ).fetchone()
    if sel is None:
        return None
    # serialize per edition: two concurrent replacements must not race to
    # the same best candidate (one would hit the unique constraint)
    conn.execute(
        "SELECT edition_id FROM editions WHERE edition_id = %s FOR UPDATE",
        (sel["edition_id"],),
    )
    if sel["replaced_at"] is not None:
        # idempotent: a stale-UI double click returns the existing swap
        return {"replaced": True, "already": True}
    from mytube import store

    if confirmed_gone:
        store.mark_unavailable(conn, [sel["video_id"]])
    pool = eligible_pool(conn, settings, user_id)
    in_edition = {
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM selections WHERE edition_id = %s",
            (sel["edition_id"],),
        ).fetchall()
    }
    now = datetime.now(UTC)
    replace_settings = get_user_settings(conn, user_id)
    penalty = replace_settings["staleness_penalty_per_day"]
    typical = typical_watched_duration(conn, user_id)
    repl_half_lives = channel_half_lives(conn, settings)

    def adjusted(c: dict) -> float:
        base = c["score"]
        if c["first_selected_at"] is not None:
            days = max((now - c["first_selected_at"]).total_seconds() / 86400.0, 0.0)
            base -= penalty * days
        base -= settings.overpromise_penalty_weight * (c.get("overpromise") or 0)
        base -= _duration_fit_penalty(
            c.get("duration_seconds"), typical,
            replace_settings.get("duration_fit_weight") or 0,
        )
        # a replacement card must be judged on freshness too, or swapping a
        # dead card silently reintroduces the staleness we just fixed
        base -= _freshness_penalty(
            c.get("published_at"), now,
            _half_life_for(c, replace_settings, settings, repl_half_lives),
            replace_settings.get("freshness_weight")
            if replace_settings.get("freshness_weight") is not None
            else settings.freshness_weight,
        )
        return base

    candidates = [
        c for c in pool
        if c["video_id"] not in in_edition
        and c["score"] is not None
        and c["unclicked_impressions"] < settings.fatigue_impressions
        # AC-31: trending stays capped at the wildcard quota — a wildcard
        # candidate may only backfill a dead wildcard slot
        and (c.get("provenance") != "wildcard" or sel["slot_class"] == "wildcard")
    ]
    # Insert-then-validate per candidate: the row goes in FIRST (savepoint),
    # then the FULL predicate — including a fresh fatigue recount — runs, so
    # anything committed after the pool read is seen (READ COMMITTED reads
    # latest at statement time); an invalid pick rolls back to the savepoint
    # and the next candidate is tried.
    stage2_ids = [c["video_id"] for c in candidates]
    inserted = None
    for candidate in sorted(
        candidates, key=lambda c: (-adjusted(c), c["video_id"])
    ):
        conn.execute("SAVEPOINT repl_candidate")
        row = conn.execute(
            """
            INSERT INTO selections
                (edition_id, user_id, video_id, position, slot_class, blind,
                 policy_version, sampling_stratum, propensity, rng_seed,
                 draw_index, eligible_set_hash, eligible_snapshot, score)
            VALUES (%s, %s, %s, %s, %s, FALSE, %s,
                    'deterministic_replacement', 1.0, NULL, NULL, %s, %s, %s)
            RETURNING selection_id
            """,
            (
                sel["edition_id"], user_id, candidate["video_id"],
                sel["position"] + 1000,    # appended; UI orders by position
                candidate.get("provenance") or "subscription",
                settings.policy_version, _hash_ids(stage2_ids),
                json.dumps(stage2_ids), candidate["score"],
            ),
        ).fetchone()
        still_ok = conn.execute(
            """
            SELECT 1 FROM videos v
            WHERE v.video_id = %(vid)s
              AND v.metadata_state = 'fresh'
              AND v.duration_seconds > %(shorts)s
              AND v.tombstoned_at IS NULL
              AND COALESCE(v.first_source, '') NOT IN ('takeout', 'likes')
              AND (
                  EXISTS (SELECT 1 FROM subscriptions sub
                          WHERE sub.user_id = %(uid)s
                            AND sub.channel_id = v.channel_id)
                  OR EXISTS (SELECT 1 FROM channels cx
                             WHERE cx.channel_id = v.channel_id AND cx.expanded)
                  OR EXISTS (SELECT 1 FROM search_sightings ss
                             WHERE ss.video_id = v.video_id
                               AND ss.user_id = %(uid)s)
                  OR v.first_source = 'trending'
              )
              AND NOT EXISTS (SELECT 1 FROM user_channel_prefs p
                              WHERE p.user_id = %(uid)s
                                AND p.channel_id = v.channel_id AND p.blocked)
              AND NOT EXISTS (SELECT 1 FROM watch_events w
                              WHERE w.user_id = %(uid)s
                                AND w.video_id = v.video_id)
              AND NOT EXISTS (SELECT 1 FROM like_events l
                              WHERE l.user_id = %(uid)s
                                AND l.video_id = v.video_id)
              AND NOT EXISTS (SELECT 1 FROM feedback_events f
                              WHERE f.user_id = %(uid)s
                                AND f.video_id = v.video_id
                                AND f.kind = 'not_interested')
              AND (SELECT count(*) FROM impressions i
                   JOIN selections s2 ON s2.selection_id = i.selection_id
                   WHERE s2.user_id = %(uid)s AND s2.video_id = %(vid)s
                     AND s2.selection_id != %(new_sid)s
                     AND NOT EXISTS (SELECT 1 FROM interactions x
                                     WHERE x.selection_id = s2.selection_id
                                       AND x.kind = 'play')
                  ) < %(fatigue)s
            """,
            {"vid": candidate["video_id"], "uid": user_id,
             "shorts": settings.shorts_max_seconds,
             "fatigue": settings.fatigue_impressions,
             "new_sid": row["selection_id"]},
        ).fetchone()
        if still_ok:
            conn.execute("RELEASE SAVEPOINT repl_candidate")
            inserted = {"selection_id": row["selection_id"],
                        "video_id": candidate["video_id"]}
            break
        conn.execute("ROLLBACK TO SAVEPOINT repl_candidate")
        # release after rollback too: reusing the name without releasing
        # stacks a new subtransaction per rejected candidate
        conn.execute("RELEASE SAVEPOINT repl_candidate")
    # the original is retired once the outcome is decided; with no valid
    # replacement it is still marked (the card is dead either way)
    conn.execute(
        "UPDATE selections SET replaced_at = now() WHERE selection_id = %s",
        (selection_id,),
    )
    if inserted is None:
        return {"replaced": False}
    return {"replaced": True, "selection_id": inserted["selection_id"],
            "video_id": inserted["video_id"]}
