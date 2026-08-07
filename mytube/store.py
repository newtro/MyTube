"""Database operations for Phase 0.

Functions take an open connection and never commit — the CLI commits once
per command so each command is atomic. Bulk inserts use single
INSERT..SELECT FROM unnest() statements so rowcount is accurate.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import numpy as np
import psycopg

from mytube.config import Settings
from mytube.takeout import TakeoutLike, TakeoutWatch
from mytube.util import classify_short
from mytube.youtube_api import HydratedVideo, SubscriptionChannel

LOCAL_OWNER_SUB = "local-owner"


def get_or_create_owner(conn: psycopg.Connection) -> int:
    """Resolve the owner for CLI/dev use.

    Phase 2's OIDC sign-in ADOPTS the 'local-owner' row (re-keys its
    google_sub to the real identity), so after adoption this must return
    that same user — NOT insert a fresh phantom 'local-owner' (that fork
    would silently split sync/CLI writes from the real account).
    """
    row = conn.execute(
        "SELECT user_id FROM users WHERE google_sub = %s", (LOCAL_OWNER_SUB,)
    ).fetchone()
    if row:
        return row["user_id"]
    rows = conn.execute(
        "SELECT user_id FROM users ORDER BY user_id"
    ).fetchall()
    if len(rows) == 1:
        return rows[0]["user_id"]        # the adopted owner
    if not rows:
        row = conn.execute(
            "INSERT INTO users (google_sub, display_name) "
            "VALUES (%s, %s) RETURNING user_id",
            (LOCAL_OWNER_SUB, "Owner"),
        ).fetchone()
        return row["user_id"]
    raise RuntimeError(
        "multiple users exist and none is 'local-owner' — the CLI owner is "
        "ambiguous; pass an explicit user or run as the web identity"
    )


def upsert_channel(
    conn: psycopg.Connection,
    channel_id: str,
    *,
    title: str | None = None,
    source: str | None = None,
    api_fetched: bool = False,
) -> None:
    conn.execute(
        """
        INSERT INTO channels (channel_id, title, source, fetched_at)
        VALUES (%s, %s, %s, CASE WHEN %s THEN now() END)
        ON CONFLICT (channel_id) DO UPDATE SET
            title = COALESCE(EXCLUDED.title, channels.title),
            fetched_at = COALESCE(EXCLUDED.fetched_at, channels.fetched_at)
        """,
        (channel_id, title, source, api_fetched),
    )


def upsert_video_stub(
    conn: psycopg.Connection,
    video_id: str,
    *,
    first_source: str,
    channel_id: str | None = None,
    title: str | None = None,
    published_at: datetime | None = None,
) -> bool:
    """Register a video id pre-hydration. Returns True if the row is new.

    first_source is first-touch: it never changes once set (AC-32 groundwork).
    """
    row = conn.execute(
        """
        INSERT INTO videos (video_id, channel_id, title, published_at, first_source)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (video_id) DO UPDATE SET
            channel_id   = COALESCE(videos.channel_id, EXCLUDED.channel_id),
            title        = COALESCE(videos.title, EXCLUDED.title),
            published_at = COALESCE(videos.published_at, EXCLUDED.published_at)
        RETURNING (xmax = 0) AS inserted
        """,
        (video_id, channel_id, title, published_at, first_source),
    ).fetchone()
    return bool(row["inserted"])


def record_watches(
    conn: psycopg.Connection, user_id: int, watches: list[TakeoutWatch], *, source: str
) -> int:
    """Insert watch events idempotently (AC-26). Returns newly inserted count."""
    if not watches:
        return 0
    cur = conn.execute(
        """
        INSERT INTO watch_events
            (user_id, video_id, watched_at, source, title_snapshot, channel_title_snapshot)
        SELECT %s, u.video_id, u.watched_at, %s, u.title, u.channel_title
        FROM unnest(
            %s::text[], %s::timestamptz[], %s::text[], %s::text[]
        ) AS u(video_id, watched_at, title, channel_title)
        ON CONFLICT (user_id, video_id, watched_at) DO NOTHING
        """,
        (
            user_id,
            source,
            [w.video_id for w in watches],
            [w.watched_at for w in watches],
            [w.title for w in watches],
            [w.channel_title for w in watches],
        ),
    )
    return cur.rowcount


def record_likes(
    conn: psycopg.Connection, user_id: int, likes: list[TakeoutLike], *, source: str
) -> int:
    if not likes:
        return 0
    cur = conn.execute(
        """
        INSERT INTO like_events (user_id, video_id, liked_at, source)
        SELECT %s, u.video_id, u.liked_at, %s
        FROM unnest(%s::text[], %s::timestamptz[]) AS u(video_id, liked_at)
        ON CONFLICT (user_id, video_id, source) DO NOTHING
        """,
        (
            user_id,
            source,
            [like.video_id for like in likes],
            [like.liked_at for like in likes],
        ),
    )
    return cur.rowcount


def upsert_subscriptions(
    conn: psycopg.Connection,
    user_id: int,
    channels: list[SubscriptionChannel],
    *,
    source: str = "api",
) -> int:
    new = 0
    for ch in channels:
        upsert_channel(
            conn, ch.channel_id, title=ch.title, source=source,
            api_fetched=(source == "api"),
        )
        row = conn.execute(
            """
            INSERT INTO subscriptions (user_id, channel_id, title, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, channel_id) DO UPDATE SET
                title = COALESCE(EXCLUDED.title, subscriptions.title),
                last_seen_at = now()
            RETURNING (xmax = 0) AS inserted
            """,
            (user_id, ch.channel_id, ch.title, source),
        ).fetchone()
        new += 1 if row["inserted"] else 0
    return new


def subscription_channels(conn: psycopg.Connection, user_id: int) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT channel_id, title FROM subscriptions
        WHERE user_id = %s ORDER BY channel_id
        """,
        (user_id,),
    ).fetchall()


def videos_needing_hydration(conn: psycopg.Connection, limit: int | None = None) -> list[str]:
    """Stubs, plus hydrated rows with unknown duration (upcoming/live streams
    report P0D) that are due a re-check — otherwise they'd stay
    candidate-ineligible forever."""
    sql = """
        SELECT video_id FROM videos
        WHERE metadata_state = 'stub'
           OR (metadata_state = 'fresh' AND duration_seconds IS NULL
               AND fetched_at < now() - interval '6 hours')
        ORDER BY first_seen_at
    """
    params: tuple = ()
    if limit is not None:
        sql += " LIMIT %s"
        params = (limit,)
    return [r["video_id"] for r in conn.execute(sql, params).fetchall()]


# SQL fragment: a video SOMEONE could currently be served (plan §5 "live
# candidate"): at least one user who hasn't watched/liked it can reach it
# through any acquisition arm — their subscriptions, an expanded channel
# (FLOW-24), search (FLOW-13), or trending (FLOW-25). Per-user correlation
# matters, or one user's watch would kill liveness for everyone.
# Parameters: shorts_max.
_LIVE_CANDIDATE_SQL = """
    v.tombstoned_at IS NULL
    AND (
    -- a card in a CURRENTLY-SERVED edition is live regardless of reach:
    -- retention must never strip metadata out from under a ready edition
    EXISTS (
        SELECT 1 FROM selections sel
        JOIN editions e ON e.edition_id = sel.edition_id
        WHERE sel.video_id = v.video_id AND e.state = 'ready'
          AND sel.replaced_at IS NULL
    )
    OR (
    v.duration_seconds > %(shorts_max)s
    AND COALESCE(v.first_source, '') NOT IN ('takeout', 'likes')
    AND EXISTS (
        SELECT 1 FROM users u
        WHERE NOT EXISTS (
              SELECT 1 FROM watch_events w
              WHERE w.user_id = u.user_id AND w.video_id = v.video_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM like_events l
              WHERE l.user_id = u.user_id AND l.video_id = v.video_id
          )
          AND (
              EXISTS (SELECT 1 FROM subscriptions s
                      WHERE s.user_id = u.user_id
                        AND s.channel_id = v.channel_id)
              OR EXISTS (SELECT 1 FROM channels cx
                         WHERE cx.channel_id = v.channel_id AND cx.expanded)
              OR EXISTS (SELECT 1 FROM search_sightings ss
                         WHERE ss.video_id = v.video_id
                           AND ss.user_id = u.user_id)
              OR v.first_source = 'trending'
          )
    )
    ))
"""


def videos_needing_hydration_due(
    conn: psycopg.Connection, *, coalesce_minutes: int, limit: int | None = None
) -> list[str]:
    """Job-facing variant of videos_needing_hydration with the §7 coalescing
    window: fresh stubs wait up to ~15 min so they batch into 50-id calls,
    unless a full batch is already pending. refresh_pending (retention) and
    stale unknown-duration rows are always due."""
    pending_stubs = conn.execute(
        "SELECT count(*) AS n FROM videos WHERE metadata_state = 'stub'"
    ).fetchone()["n"]
    stub_clause = (
        "TRUE" if pending_stubs >= 50
        else "first_seen_at < now() - make_interval(mins => %(coalesce)s)"
    )
    sql = f"""
        SELECT video_id FROM videos
        WHERE (metadata_state = 'stub' AND {stub_clause})
           OR metadata_state = 'refresh_pending'
           OR (metadata_state = 'fresh' AND duration_seconds IS NULL
               AND fetched_at < now() - interval '6 hours')
        ORDER BY first_seen_at
    """
    params: dict[str, Any] = {"coalesce": coalesce_minutes}
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    return [r["video_id"] for r in conn.execute(sql, params).fetchall()]


def embeddable_video_ids(
    conn: psycopg.Connection, *, shorts_max_seconds: int, limit: int | None = None
) -> list[str]:
    """Videos worth embedding with the model of record: live candidates (any
    user) plus every taste-item video, hydrated, missing rows filtered later
    by the hash checker."""
    sql = f"""
        SELECT v.video_id FROM videos v
        WHERE v.metadata_state = 'fresh'
          AND ({_LIVE_CANDIDATE_SQL}
               OR EXISTS (SELECT 1 FROM taste_items t WHERE t.video_id = v.video_id))
        ORDER BY v.first_seen_at DESC
    """
    params: dict[str, Any] = {"shorts_max": shorts_max_seconds}
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    return [r["video_id"] for r in conn.execute(sql, params).fetchall()]


def apply_retention(
    conn: psycopg.Connection, *, retention_days: int, shorts_max_seconds: int
) -> dict[str, int]:
    """§5 lifecycle (AC-22): expired API metadata of live candidates is
    queued for refresh; non-live videos' expiring fields are purged.

    Durable columns survive: video_id, first_source, published_at,
    duration_seconds/is_short (structural, drive the hard filter), and
    everything in watch/like/taste/embedding tables. Purged: title,
    description, tags, view_count, thumbnail_url.
    Tombstoned ('unavailable') rows are never touched.
    """
    params = {"days": retention_days, "shorts_max": shorts_max_seconds}
    refreshed = conn.execute(
        f"""
        UPDATE videos v SET metadata_state = 'refresh_pending'
        WHERE v.metadata_state = 'fresh'
          AND v.fetched_at < now() - make_interval(days => %(days)s)
          AND ({_LIVE_CANDIDATE_SQL})
        """,
        params,
    ).rowcount
    # a previously-expired video that re-entered the live pool gets refreshed
    revived = conn.execute(
        f"""
        UPDATE videos v SET metadata_state = 'refresh_pending'
        WHERE v.metadata_state = 'expired'
          AND ({_LIVE_CANDIDATE_SQL})
        """,
        params,
    ).rowcount
    purged = conn.execute(
        f"""
        UPDATE videos v SET
            metadata_state = 'expired',
            title = NULL, description = NULL, tags = NULL,
            view_count = NULL, thumbnail_url = NULL
        WHERE v.metadata_state = 'fresh'
          AND v.fetched_at < now() - make_interval(days => %(days)s)
          AND NOT ({_LIVE_CANDIDATE_SQL})
        """,
        params,
    ).rowcount
    # channel titles are API metadata too: purge stale ones for channels that
    # are neither pollable nor refreshed by the nightly sync (active
    # subscriptions get their titles re-upserted nightly)
    channels_purged = conn.execute(
        """
        UPDATE channels c SET title = NULL
        WHERE c.title IS NOT NULL
          AND c.fetched_at < now() - make_interval(days => %(days)s)
          AND NOT c.expanded
          AND NOT EXISTS (SELECT 1 FROM subscriptions s WHERE s.channel_id = c.channel_id)
        """,
        params,
    ).rowcount
    return {
        "refresh_queued": refreshed,
        "revived": revived,
        "purged": purged,
        "channels_purged": channels_purged,
    }


def ops_backlogs(
    conn: psycopg.Connection, *, model_id: str, shorts_max_seconds: int, quota_day
) -> dict[str, Any]:
    """Backlog counts + hydration batch utilization for the ops view (AC-17).

    pending_embed counts only videos the embed job would actually process
    (live candidates + taste videos) — counting every fresh video would
    show a permanently-growing queue that never drains."""
    counts = {}
    for name, sql in [
        ("stubs_pending", "SELECT count(*) AS n FROM videos WHERE metadata_state = 'stub'"),
        (
            "refresh_pending",
            "SELECT count(*) AS n FROM videos WHERE metadata_state = 'refresh_pending'",
        ),
        (
            "open_alerts",
            "SELECT count(*) AS n FROM ops_alerts WHERE resolved_at IS NULL",
        ),
    ]:
        counts[name] = conn.execute(sql).fetchone()["n"]
    # missing-only: rows whose stored input hash drifted are re-embedded by
    # the job but are not counted here (would require rebuilding every input)
    counts["pending_embed_missing"] = conn.execute(
        f"""
        SELECT count(*) AS n FROM videos v
        WHERE v.metadata_state = 'fresh'
          AND ({_LIVE_CANDIDATE_SQL}
               OR EXISTS (SELECT 1 FROM taste_items t WHERE t.video_id = v.video_id))
          AND NOT EXISTS (
              SELECT 1 FROM video_embeddings e
              WHERE e.video_id = v.video_id AND e.model_id = %(model_id)s
          )
        """,
        {"model_id": model_id, "shorts_max": shorts_max_seconds},
    ).fetchone()["n"]
    util = conn.execute(
        """
        SELECT count(*) AS batches,
               COALESCE(avg(ids_requested), 0) AS avg_batch_size
        FROM hydration_batches
        WHERE quota_day = %s
        """,
        (quota_day,),
    ).fetchone()
    counts["hydration_batches_today"] = util["batches"]
    counts["hydration_avg_batch_size"] = float(util["avg_batch_size"])
    return counts


def record_hydration_batch(
    conn: psycopg.Connection, *, quota_day, ids_requested: int, ids_found: int
) -> None:
    conn.execute(
        """
        INSERT INTO hydration_batches (quota_day, ids_requested, ids_found)
        VALUES (%s, %s, %s)
        """,
        (quota_day, ids_requested, ids_found),
    )


def apply_hydration(
    conn: psycopg.Connection, video: HydratedVideo, *, shorts_max_seconds: int
) -> bool | None:
    """Store fetched metadata; classify Shorts tri-state (AC-18).

    Returns True (Short), False (confirmed long-form), or None (duration
    unknown — e.g. upcoming live; stays candidate-ineligible).
    """
    if video.channel_id:
        upsert_channel(
            conn, video.channel_id, title=video.channel_title, source="api",
            api_fetched=True,
        )
    is_short = classify_short(video.duration_seconds, shorts_max_seconds)
    conn.execute(
        """
        UPDATE videos SET
            channel_id       = COALESCE(%s, channel_id),
            title            = COALESCE(%s, title),
            description      = %s,
            tags             = %s,
            duration_seconds = %s,
            is_short         = %s,
            published_at     = COALESCE(%s, published_at),
            view_count       = %s,
            thumbnail_url    = %s,
            metadata_state   = 'fresh',
            fetched_at       = now()
        WHERE video_id = %s
        """,
        (
            video.channel_id,
            video.title,
            video.description,
            video.tags,
            video.duration_seconds,
            is_short,
            video.published_at,
            video.view_count,
            video.thumbnail_url,
            video.video_id,
        ),
    )
    return is_short


def mark_unavailable(conn: psycopg.Connection, video_ids: list[str]) -> None:
    """Confirmed deleted/private: tombstone (§5)."""
    if not video_ids:
        return
    conn.execute(
        """
        UPDATE videos SET metadata_state = 'unavailable', tombstoned_at = now()
        WHERE video_id = ANY(%s)
        """,
        (video_ids,),
    )


def rebuild_taste_items(
    conn: psycopg.Connection, user_id: int, settings: Settings
) -> dict[str, int]:
    """Rebuild like/watch-sourced taste items from durable events (§6, AC-25/30).

    - likes -> strong positives (weight taste_like_weight)
    - watches -> weak positives; rewatches add taste_rewatch_bonus each,
      capped at taste_max_weight; recency handled at scoring time from
      last_event_at
    - a video that is both liked and watched keeps only the like row
    - Shorts excluded when exclude_shorts_from_taste (owner requirement)

    Membership is deliberately INDEPENDENT of metadata freshness: retention
    expires/purges a taste video's API metadata after ~30 days (§5), but the
    taste row must survive — duration_seconds/is_short are durable columns
    and the embedding row is retained, so scoring keeps working. (Requiring
    metadata_state='fresh' here would silently gut the taste set on the
    first nightly rebuild after a retention pass.) Videos never hydrated at
    all (duration unknown) are excluded only when excluding Shorts, since
    they can't be verified as long-form.

    Idempotent: deletes and re-inserts the 'like'/'watch' sourced rows only,
    so future feedback-sourced taste items survive rebuilds.
    """
    conn.execute(
        "DELETE FROM taste_items WHERE user_id = %s AND source IN ('like', 'watch')",
        (user_id,),
    )
    # Exclusion is evaluated against the CURRENT threshold (not the
    # hydration-time is_short snapshot) so a config change takes effect on
    # the next rebuild. Unknown durations are excluded when excluding —
    # they might be Shorts.
    if settings.exclude_shorts_from_taste:
        shorts_filter = "AND v.duration_seconds > %(shorts_max)s"
    else:
        shorts_filter = ""
    params: dict[str, Any] = {
        "user_id": user_id,
        "like_weight": settings.taste_like_weight,
        "watch_weight": settings.taste_watch_weight,
        "rewatch_bonus": settings.taste_rewatch_bonus,
        "max_weight": settings.taste_max_weight,
        "max_watch_items": settings.taste_max_watch_items,
        "shorts_max": settings.shorts_max_seconds,
    }
    likes = conn.execute(
        f"""
        INSERT INTO taste_items
            (user_id, video_id, polarity, weight, source, last_event_at, event_count)
        SELECT l.user_id, l.video_id, 1, %(like_weight)s, 'like',
               MAX(COALESCE(l.liked_at, l.created_at)), COUNT(*)
        FROM like_events l
        JOIN videos v ON v.video_id = l.video_id
        WHERE l.user_id = %(user_id)s
          AND v.metadata_state != 'unavailable' {shorts_filter}
        GROUP BY l.user_id, l.video_id
        """,
        params,
    ).rowcount
    watches = conn.execute(
        f"""
        INSERT INTO taste_items
            (user_id, video_id, polarity, weight, source, last_event_at, event_count)
        SELECT w.user_id, w.video_id, 1,
               LEAST(%(watch_weight)s * (1 + %(rewatch_bonus)s * (COUNT(*) - 1)),
                     %(max_weight)s),
               'watch', MAX(w.watched_at), COUNT(*)
        FROM watch_events w
        JOIN videos v ON v.video_id = w.video_id
        WHERE w.user_id = %(user_id)s
          AND v.metadata_state != 'unavailable' {shorts_filter}
          AND NOT EXISTS (
              SELECT 1 FROM taste_items t
              WHERE t.user_id = w.user_id AND t.video_id = w.video_id AND t.source = 'like'
          )
        GROUP BY w.user_id, w.video_id
        ORDER BY MAX(w.watched_at) DESC
        LIMIT %(max_watch_items)s
        """,
        params,
    ).rowcount
    return {"like_items": likes, "watch_items": watches}


def taste_items(conn: psycopg.Connection, user_id: int) -> list[dict[str, Any]]:
    return conn.execute(
        """
        SELECT video_id, polarity, weight, source, last_event_at, event_count
        FROM taste_items WHERE user_id = %s ORDER BY taste_item_id
        """,
        (user_id,),
    ).fetchall()


def candidate_sample(
    conn: psycopg.Connection, user_id: int, *, limit: int,
    shorts_max_seconds: int,
) -> list[dict[str, Any]]:
    """Unwatched, un-liked, hydrated uploads from THIS user's subscriptions.

    The AC-3/AC-18 hard filter evaluates duration against the CURRENT
    threshold (unknown durations excluded by `>`); tombstoned rows are out.
    Watched AND liked videos are both excluded so the candidate set is
    provably disjoint from the taste seed (a taste video among candidates
    would gift itself a top-20 slot via self-similarity). Scoped to the
    user's subscriptions so a second user's pull can't leak channels.
    """
    return conn.execute(
        """
        SELECT v.video_id, v.title, c.title AS channel_title,
               v.published_at, v.duration_seconds
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.metadata_state = 'fresh'
          AND v.duration_seconds > %s
          AND v.tombstoned_at IS NULL
          AND v.first_source = 'rss'
          AND EXISTS (
              SELECT 1 FROM subscriptions s
              WHERE s.user_id = %s AND s.channel_id = v.channel_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM watch_events w
              WHERE w.user_id = %s AND w.video_id = v.video_id
          )
          AND NOT EXISTS (
              SELECT 1 FROM like_events l
              WHERE l.user_id = %s AND l.video_id = v.video_id
          )
        ORDER BY v.published_at DESC NULLS LAST, v.video_id
        LIMIT %s
        """,
        (shorts_max_seconds, user_id, user_id, user_id, limit),
    ).fetchall()


def videos_meta_for_inputs(
    conn: psycopg.Connection, video_ids: list[str]
) -> list[dict[str, Any]]:
    """Hydrated metadata needed to build embedding inputs for ``video_ids``.

    Used at benchmark-freeze time so the frozen artifact captures the exact
    embedding input text per video (reproducibility of the gate)."""
    return conn.execute(
        """
        SELECT v.video_id, v.title, c.title AS channel_title, v.description, v.tags
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.video_id = ANY(%s) AND v.metadata_state = 'fresh'
        ORDER BY v.video_id
        """,
        (video_ids,),
    ).fetchall()


def videos_for_embedding(
    conn: psycopg.Connection, video_ids: list[str]
) -> list[dict[str, Any]]:
    """Hydrated metadata for building embedding inputs.

    Deliberately does NOT filter out already-embedded rows: the caller
    (embed_inputs_map) decides what to embed via the input-hash comparison,
    so metadata drift gets re-embedded even by the standalone `mytube
    embed` command."""
    return conn.execute(
        """
        SELECT v.video_id, v.title, c.title AS channel_title, v.description, v.tags
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.video_id = ANY(%s)
          AND v.metadata_state = 'fresh'
        ORDER BY v.video_id
        """,
        (video_ids,),
    ).fetchall()


def store_embedding(
    conn: psycopg.Connection,
    video_id: str,
    model_id: str,
    embedding: np.ndarray,
    *,
    input_text: str | None = None,
) -> None:
    """Store a model-tagged vector. Only a hash of the input text is kept —
    raw API-derived text would sidestep the expiring-metadata retention
    posture (§5/ASM-3); the hash still lets an audit detect input drift."""
    vec = np.asarray(embedding, dtype=np.float32)
    # `is not None` (not truthiness): an empty input must still hash, or the
    # stale-detector would re-embed it on every single run
    input_sha256 = (
        hashlib.sha256(input_text.encode("utf-8")).hexdigest()
        if input_text is not None
        else None
    )
    conn.execute(
        """
        INSERT INTO video_embeddings (video_id, model_id, dims, embedding, input_sha256)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (video_id, model_id) DO UPDATE SET
            dims = EXCLUDED.dims,
            embedding = EXCLUDED.embedding,
            input_sha256 = EXCLUDED.input_sha256,
            embedded_at = now()
        """,
        (video_id, model_id, vec.shape[0], vec, input_sha256),
    )


def embedding_missing_ids(
    conn: psycopg.Connection, model_id: str, video_ids: list[str]
) -> list[str]:
    """Of ``video_ids``, those still lacking this model's embedding row."""
    have = {
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM video_embeddings "
            "WHERE model_id = %s AND video_id = ANY(%s)",
            (model_id, video_ids),
        ).fetchall()
    }
    return [v for v in video_ids if v not in have]


def embedding_stale_or_missing_ids(
    conn: psycopg.Connection, model_id: str, inputs: dict[str, str]
) -> list[str]:
    """Ids whose embedding row is absent OR whose stored input_sha256 does
    not match the given input text — those vectors were built from different
    metadata and must be re-embedded, or the gate would silently score stale
    vectors against the frozen artifact."""
    stored = {
        r["video_id"]: r["input_sha256"]
        for r in conn.execute(
            "SELECT video_id, input_sha256 FROM video_embeddings "
            "WHERE model_id = %s AND video_id = ANY(%s)",
            (model_id, list(inputs.keys())),
        ).fetchall()
    }
    stale: list[str] = []
    for vid, text in inputs.items():
        expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if stored.get(vid) != expected:   # absent, NULL-hash, or drifted
            stale.append(vid)
    return stale


def reconcile_subscriptions(
    conn: psycopg.Connection, user_id: int, current_channel_ids: list[str]
) -> int:
    """After a COMPLETE subscription pull, drop rows no longer subscribed.

    Must only run on a full snapshot (a partial page-set would mass-delete),
    which the CLI guarantees by calling this only when pagination finished
    without error. Returns removed count."""
    cur = conn.execute(
        """
        DELETE FROM subscriptions
        WHERE user_id = %s AND channel_id != ALL(%s)
        """,
        (user_id, current_channel_ids or [""]),
    )
    return cur.rowcount


def _vector_to_array(value) -> np.ndarray:
    """The pgvector adapter yields np.ndarray or pgvector.Vector depending on
    protocol/column typing (untyped `vector` columns load as Vector objects);
    normalize both to float32 ndarray."""
    if isinstance(value, np.ndarray):
        return value.astype(np.float32)
    to_numpy = getattr(value, "to_numpy", None)
    if callable(to_numpy):
        return to_numpy().astype(np.float32)
    return np.asarray(list(value), dtype=np.float32)


def load_embeddings(
    conn: psycopg.Connection, model_id: str, video_ids: list[str]
) -> dict[str, np.ndarray]:
    rows = conn.execute(
        """
        SELECT video_id, embedding FROM video_embeddings
        WHERE model_id = %s AND video_id = ANY(%s)
        """,
        (model_id, video_ids),
    ).fetchall()
    return {r["video_id"]: _vector_to_array(r["embedding"]) for r in rows}


def get_sync_state(conn: psycopg.Connection, user_id: int) -> dict[str, Any]:
    """Per-user private-sync progress row (created on first access)."""
    row = conn.execute(
        "SELECT * FROM user_sync_state WHERE user_id = %s", (user_id,)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "INSERT INTO user_sync_state (user_id) VALUES (%s) RETURNING *",
            (user_id,),
        ).fetchone()
    return row


def update_likes_sync_state(
    conn: psycopg.Connection,
    user_id: int,
    *,
    next_page_token: str | None,
    backfill_done: bool | None = None,
    full_scan_completed: bool = False,
    mark_dirty: bool = False,
    reset_dirty: bool = False,
) -> None:
    """``mark_dirty`` poisons the in-progress multi-night scan (malformed
    items seen); ``reset_dirty`` clears it when a pass concludes."""
    conn.execute(
        """
        INSERT INTO user_sync_state
            (user_id, likes_next_page_token, likes_backfill_done,
             likes_last_synced_at, likes_last_full_scan_at, likes_scan_dirty,
             updated_at)
        VALUES (%(uid)s, %(token)s, COALESCE(%(done)s, FALSE),
                now(), CASE WHEN %(full)s THEN now() END, %(dirty)s, now())
        ON CONFLICT (user_id) DO UPDATE SET
            likes_next_page_token = %(token)s,
            likes_backfill_done = COALESCE(%(done)s, user_sync_state.likes_backfill_done),
            likes_last_synced_at = now(),
            likes_last_full_scan_at = CASE WHEN %(full)s THEN now()
                ELSE user_sync_state.likes_last_full_scan_at END,
            likes_scan_dirty = CASE
                WHEN %(reset)s THEN FALSE
                WHEN %(dirty)s THEN TRUE
                ELSE user_sync_state.likes_scan_dirty END,
            updated_at = now()
        """,
        {"uid": user_id, "token": next_page_token, "done": backfill_done,
         "full": full_scan_completed, "dirty": mark_dirty, "reset": reset_dirty},
    )


def status_counts(conn: psycopg.Connection, user_id: int) -> dict[str, Any]:
    counts: dict[str, Any] = {}
    for name, sql, params in [
        ("videos_total", "SELECT count(*) AS n FROM videos", ()),
        (
            "videos_by_state",
            "SELECT metadata_state, count(*) AS n FROM videos GROUP BY 1 ORDER BY 1",
            (),
        ),
        ("videos_shorts", "SELECT count(*) AS n FROM videos WHERE is_short", ()),
        (
            "videos_unknown_duration",
            "SELECT count(*) AS n FROM videos "
            "WHERE metadata_state = 'fresh' AND duration_seconds IS NULL",
            (),
        ),
        ("channels", "SELECT count(*) AS n FROM channels", ()),
        (
            "subscriptions",
            "SELECT count(*) AS n FROM subscriptions WHERE user_id = %s",
            (user_id,),
        ),
        (
            "watch_events",
            "SELECT count(*) AS n FROM watch_events WHERE user_id = %s",
            (user_id,),
        ),
        (
            "like_events",
            "SELECT count(*) AS n FROM like_events WHERE user_id = %s",
            (user_id,),
        ),
        (
            "taste_items",
            "SELECT source, count(*) AS n FROM taste_items WHERE user_id = %s "
            "GROUP BY 1 ORDER BY 1",
            (user_id,),
        ),
        (
            "embeddings_by_model",
            "SELECT model_id, count(*) AS n FROM video_embeddings GROUP BY 1 ORDER BY 1",
            (),
        ),
    ]:
        rows = conn.execute(sql, params).fetchall()
        if len(rows) == 1 and "n" in rows[0] and len(rows[0]) == 1:
            counts[name] = rows[0]["n"]
        else:
            counts[name] = rows
    return counts
