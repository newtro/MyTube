"""RSS poller (FLOW-11): zero-quota upload detection, staggered 30-60 min.

Each channel carries its own next_poll_at; a stable per-channel jitter
spreads the fleet across the interval so polls never stampede. Failures are
isolated per channel (AC-19): one broken feed backs off alone and alerts
after 24 hours of consecutive failures, while everything else keeps
polling.
"""

from __future__ import annotations

import zlib
from datetime import timedelta

import httpx
import psycopg

from mytube import rss, store
from mytube.config import Settings
from mytube.scheduler import JobContext, raise_alert, resolve_alert


def base_interval_minutes(settings: Settings, channel_id: str) -> float:
    """Stable stagger: hash the channel id into [min, max] minutes."""
    span = max(settings.rss_poll_max_minutes - settings.rss_poll_min_minutes, 0)
    fraction = (zlib.crc32(channel_id.encode()) % 1000) / 1000
    return settings.rss_poll_min_minutes + span * fraction


def next_poll_delay(
    settings: Settings, channel_id: str, *, consecutive_failures: int = 0
) -> timedelta:
    """Base stagger, doubled per consecutive failure (capped at 2^4)."""
    minutes = base_interval_minutes(settings, channel_id)
    minutes *= 2 ** min(consecutive_failures, 4)
    return timedelta(minutes=minutes)


def ensure_poll_state_rows(conn: psycopg.Connection) -> dict[str, int]:
    """Every pollable channel (subscribed by anyone, or tagged expanded —
    FLOW-24 reuses this path) gets a poll-state row; rows for channels that
    are no longer pollable (unsubscribed, un-expanded) are pruned so dropped
    channels stop consuming polls and hydration quota forever."""
    added = conn.execute(
        """
        INSERT INTO channel_poll_state (channel_id)
        SELECT c.channel_id FROM channels c
        WHERE c.expanded
           OR EXISTS (SELECT 1 FROM subscriptions s WHERE s.channel_id = c.channel_id)
        ON CONFLICT (channel_id) DO NOTHING
        """
    ).rowcount
    pruned_rows = conn.execute(
        """
        DELETE FROM channel_poll_state p
        USING channels c
        WHERE c.channel_id = p.channel_id
          AND NOT c.expanded
          AND NOT EXISTS (
              SELECT 1 FROM subscriptions s WHERE s.channel_id = p.channel_id
          )
        RETURNING p.channel_id
        """
    ).fetchall()
    # a pruned channel's open failure alert would otherwise stay open forever
    for row in pruned_rows:
        resolve_alert(conn, "rss_failure", row["channel_id"])
    return {"added": added, "pruned": len(pruned_rows)}


def _record_success(
    conn: psycopg.Connection, settings: Settings, channel_id: str
) -> None:
    delay = next_poll_delay(settings, channel_id)
    conn.execute(
        """
        UPDATE channel_poll_state SET
            last_polled_at = now(), last_success_at = now(),
            consecutive_failures = 0, failing_since = NULL, alerted_at = NULL,
            next_poll_at = now() + %s
        WHERE channel_id = %s
        """,
        (delay, channel_id),
    )
    resolve_alert(conn, "rss_failure", channel_id)


def _record_failure(
    conn: psycopg.Connection, settings: Settings, channel_id: str, message: str
) -> None:
    row = conn.execute(
        """
        UPDATE channel_poll_state SET
            last_polled_at = now(),
            consecutive_failures = consecutive_failures + 1,
            failing_since = COALESCE(failing_since, now())
        WHERE channel_id = %s
        RETURNING consecutive_failures, failing_since, alerted_at
        """,
        (channel_id,),
    ).fetchone()
    delay = next_poll_delay(
        settings, channel_id, consecutive_failures=row["consecutive_failures"]
    )
    # AC-19 timing: never let backoff push the next attempt past the
    # 24h-alert deadline, or the alert could slip to 30h+; retry shortly
    # after the deadline so the alert fires close to on time
    if row["alerted_at"] is None:
        deadline_delay = conn.execute(
            """
            SELECT GREATEST(
                %s + make_interval(hours => %s, mins => 5) - now(),
                make_interval(mins => 5)
            ) AS d
            """,
            (row["failing_since"], settings.rss_alert_after_hours),
        ).fetchone()["d"]
        delay = min(delay, deadline_delay)
    conn.execute(
        "UPDATE channel_poll_state SET next_poll_at = now() + %s WHERE channel_id = %s",
        (delay, channel_id),
    )
    # AC-19: alert after 24h of consecutive failures
    due = conn.execute(
        "SELECT %s < now() - make_interval(hours => %s) AS due",
        (row["failing_since"], settings.rss_alert_after_hours),
    ).fetchone()["due"]
    if due and row["alerted_at"] is None:
        raise_alert(
            conn, "rss_failure", channel_id,
            f"RSS feed failing for over {settings.rss_alert_after_hours}h: {message}",
        )
        conn.execute(
            "UPDATE channel_poll_state SET alerted_at = now() WHERE channel_id = %s",
            (channel_id,),
        )


def poll_due_channels(ctx: JobContext) -> dict:
    """One poller tick: poll every channel whose next_poll_at has arrived."""
    conn, settings = ctx.conn, ctx.settings
    ensure_poll_state_rows(conn)
    conn.commit()
    due = conn.execute(
        """
        SELECT p.channel_id FROM channel_poll_state p
        JOIN channels c ON c.channel_id = p.channel_id
        WHERE p.next_poll_at <= now()
          AND (c.expanded OR EXISTS (
              SELECT 1 FROM subscriptions s WHERE s.channel_id = p.channel_id
          ))
        ORDER BY p.next_poll_at
        LIMIT %s
        """,
        (settings.rss_poll_batch,),
    ).fetchall()

    polled = failures = new_videos = 0
    with httpx.Client(headers={"User-Agent": "mytube/0.1"}) as client:
        for row in due:
            # heartbeat per channel: a run of slow feeds (20s timeout each)
            # must not outlive the lease between beats
            ctx.heartbeat()
            channel_id = row["channel_id"]
            try:
                entries = rss.fetch_channel_uploads(client, channel_id)
            except rss.FeedError as exc:
                failures += 1
                _record_failure(conn, settings, channel_id, str(exc))
                conn.commit()   # isolation: this channel's failure is committed alone
                continue
            for entry in entries:
                if store.upsert_video_stub(
                    conn,
                    entry.video_id,
                    first_source="rss",
                    channel_id=entry.channel_id,
                    title=entry.title,
                    published_at=entry.published_at,
                ):
                    new_videos += 1
            _record_success(conn, settings, channel_id)
            conn.commit()
            polled += 1
    return {"polled": polled, "failures": failures, "new_videos": new_videos}
