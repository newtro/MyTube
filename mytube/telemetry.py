"""Playback telemetry (FLOW-4; AC-5/6/7/40).

Writes are idempotent on (watch_session_id, seq): duplicates and
out-of-order updates are rejected. Watched ranges are tracked separately
from the playhead and merged server-side. Positive threshold (>=70%
completion or >=20 min) records a DAY-BUCKETED durable WatchEvent — the
unique key collapses multi-tab and rewatch activity into one net signal
per (user, video, day) (AC-40). Early abandonment (<20% length-adjusted
after >=30s exposure) records a soft-negative taste item (§6/DEC-9) that
taste rebuilds preserve (they only replace like/watch rows).
"""

from __future__ import annotations

import json

import psycopg

from mytube.config import Settings


def merge_ranges(ranges: list[list[float]]) -> list[list[float]]:
    """Merge possibly-overlapping [start, end] ranges (seconds)."""
    cleaned = sorted(
        [r[0], r[1]] for r in ranges
        if isinstance(r, (list, tuple)) and len(r) == 2
        and isinstance(r[0], (int, float)) and isinstance(r[1], (int, float))
        and r[1] > r[0] >= 0
    )
    merged: list[list[float]] = []
    for start, end in cleaned:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def watched_seconds(ranges: list[list[float]]) -> float:
    return sum(end - start for start, end in ranges)


def _record_positive(
    conn: psycopg.Connection,
    settings: Settings,
    user_id: int,
    video_id: str,
    timezone: str,
) -> None:
    """Durable day-bucketed WatchEvent + title snapshot (survives expiry).

    Also upserts the taste item immediately (AC-5: "joins taste set") — the
    nightly rebuild recomputes the same row from events, but the signal
    must not wait a day (or depend on sync health) to influence scoring.
    """
    conn.execute(
        """
        INSERT INTO watch_events
            (user_id, video_id, watched_at, source, title_snapshot,
             channel_title_snapshot)
        SELECT %(uid)s, %(vid)s,
               date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s,
               'player', v.title, c.title
        FROM videos v
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE v.video_id = %(vid)s
        ON CONFLICT (user_id, video_id, watched_at) DO NOTHING
        """,
        {"uid": user_id, "vid": video_id, "tz": timezone},
    )
    conn.execute(
        """
        INSERT INTO taste_items
            (user_id, video_id, polarity, weight, source, last_event_at)
        VALUES (%s, %s, 1, %s, 'watch', now())
        ON CONFLICT (user_id, video_id, source) DO UPDATE SET
            last_event_at = now(), updated_at = now()
        """,
        (user_id, video_id, settings.taste_watch_weight),
    )


def _record_soft_negative(
    conn: psycopg.Connection, settings: Settings, user_id: int, video_id: str
) -> None:
    conn.execute(
        """
        INSERT INTO taste_items
            (user_id, video_id, polarity, weight, source, last_event_at)
        VALUES (%s, %s, -1, %s, 'abandon', now())
        ON CONFLICT (user_id, video_id, source) DO UPDATE SET
            last_event_at = now(),
            event_count = taste_items.event_count + 1,
            updated_at = now()
        """,
        (user_id, video_id, settings.taste_abandon_weight),
    )


def apply_watch_update(
    conn: psycopg.Connection,
    settings: Settings,
    user_id: int,
    *,
    watch_session_id: str,
    video_id: str,
    seq: int,
    position: float,
    ranges: list[list[float]],
    exposure_seconds: float,
    event: str,                       # heartbeat | ended | close
    selection_id: int | None = None,
    timezone: str = "UTC",
) -> dict:
    """One idempotent telemetry write. Returns {'accepted', 'completed'}."""
    if seq < 1:
        return {"accepted": False, "reason": "bad-seq"}
    # serialize concurrent tabs on the same (user, video): the positive/
    # negative decision below must see the other tab's committed outcome
    import hashlib as _hashlib

    lock_key = int.from_bytes(
        _hashlib.sha256(f"watch:{user_id}:{video_id}".encode()).digest()[:8],
        "big", signed=True,
    )
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
    duration = conn.execute(
        "SELECT duration_seconds FROM videos WHERE video_id = %s", (video_id,)
    ).fetchone()
    if duration is None:
        return {"accepted": False, "reason": "unknown-video"}
    duration_s = duration["duration_seconds"]
    if selection_id is not None:
        owned = conn.execute(
            """
            SELECT 1 FROM selections
            WHERE selection_id = %s AND user_id = %s AND video_id = %s
            """,
            (selection_id, user_id, video_id),
        ).fetchone()
        if owned is None:
            return {"accepted": False, "reason": "bad-selection"}
    row = conn.execute(
        "SELECT * FROM watch_sessions WHERE watch_session_id = %s FOR UPDATE",
        (watch_session_id,),
    ).fetchone()
    if row is None:
        # race-safe first write (two tabs can share a fresh session id)
        conn.execute(
            """
            INSERT INTO watch_sessions
                (watch_session_id, user_id, video_id, selection_id)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (watch_session_id) DO NOTHING
            """,
            (watch_session_id, user_id, video_id, selection_id),
        )
        row = conn.execute(
            "SELECT * FROM watch_sessions WHERE watch_session_id = %s FOR UPDATE",
            (watch_session_id,),
        ).fetchone()
    if row["user_id"] != user_id or row["video_id"] != video_id:
        return {"accepted": False, "reason": "session-mismatch"}
    if seq <= row["last_seq"]:
        # duplicate or out-of-order (AC-40): reject, change nothing
        return {"accepted": False, "reason": "stale-seq"}

    # clamp claimed ranges to the known duration (+2s player grace)
    incoming = list(ranges or [])
    if duration_s:
        cap = float(duration_s) + 2.0
        incoming = [
            [min(float(r[0]), cap), min(float(r[1]), cap)] for r in incoming
        ]
    merged = merge_ranges(list(row["watched_ranges"]) + incoming)
    exposure = max(float(row["exposure_seconds"]), float(exposure_seconds or 0))
    ended = event in ("ended", "close")
    conn.execute(
        """
        UPDATE watch_sessions SET
            last_seq = %s, last_position = %s, watched_ranges = %s,
            exposure_seconds = %s,
            ended_at = CASE WHEN %s THEN now() ELSE ended_at END,
            updated_at = now()
        WHERE watch_session_id = %s
        """,
        (seq, position, json.dumps(merged), exposure, ended, watch_session_id),
    )

    watched = watched_seconds(merged)

    completed = bool(row["completed"])
    # the absolute >=20-min branch also requires plausible exposure (you
    # can't watch 20 minutes with 5 minutes of wall-clock) — forged ranges
    # on unknown-duration videos must not instantly complete
    absolute_ok = (
        watched >= settings.positive_watch_seconds and exposure >= watched * 0.5
    )
    if not completed and (
        (duration_s and watched >= settings.positive_completion_ratio * duration_s)
        or absolute_ok
    ):
        completed = True
        conn.execute(
            "UPDATE watch_sessions SET completed = TRUE WHERE watch_session_id = %s",
            (watch_session_id,),
        )
        _record_positive(conn, settings, user_id, video_id, timezone)
        # a completed watch supersedes any earlier abandon signal — one NET
        # signal per (user, video) (AC-40)
        conn.execute(
            "DELETE FROM taste_items WHERE user_id = %s AND video_id = %s "
            "AND source = 'abandon'",
            (user_id, video_id),
        )

    if (
        ended
        and not completed
        and not row["negative_recorded"]
        and duration_s
        and exposure >= settings.negative_min_exposure_seconds
        and watched < settings.negative_completion_ratio * duration_s
    ):
        # cross-session guard (AC-40): if ANY session (another tab) already
        # produced today's positive signal, an abandoning tab must not add
        # a contradictory negative
        positive_today = conn.execute(
            """
            SELECT 1 FROM watch_events
            WHERE user_id = %(uid)s AND video_id = %(vid)s AND source = 'player'
              AND watched_at =
                  date_trunc('day', now() AT TIME ZONE %(tz)s) AT TIME ZONE %(tz)s
            """,
            {"uid": user_id, "vid": video_id, "tz": timezone},
        ).fetchone()
        conn.execute(
            """
            UPDATE watch_sessions SET negative_recorded = TRUE
            WHERE watch_session_id = %s
            """,
            (watch_session_id,),
        )
        if positive_today is None:
            _record_soft_negative(conn, settings, user_id, video_id)

    return {"accepted": True, "completed": completed,
            "watched_seconds": watched}


def user_timezone(conn: psycopg.Connection, user_id: int) -> str:
    row = conn.execute(
        "SELECT timezone FROM user_settings WHERE user_id = %s", (user_id,)
    ).fetchone()
    return row["timezone"] if row else "UTC"
