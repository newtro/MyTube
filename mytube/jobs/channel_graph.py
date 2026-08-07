"""Weekly channel-graph expansion (FLOW-24, AC-31).

Two edge sources:
1. Featured channels of a rotated sample of subscribed channels
   (channelSections.list contentDetails.channels, 1 metered unit per
   channel — featuredChannelsUrls was removed from the API in 2020).
2. Recurring channels: watched >= N times BY THE SAME USER (durable
   watch_events joined to video rows) while that user isn't subscribed.

New channels are tagged ``expanded`` and thereby enter zero-quota RSS
polling (the poller's pollable filter already includes them)."""

from __future__ import annotations

import time

from mytube import quota, store, youtube_api
from mytube.scheduler import JobContext


def expand_channel_graph(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    added_featured = added_recurring = 0
    stopped = None

    # --- source 2 first: zero quota -------------------------------------
    # per-user counting: three users with one watch each must NOT sum to an
    # expansion, and user A's subscription must not suppress user B's signal
    recurring = conn.execute(
        """
        SELECT DISTINCT channel_id FROM (
            SELECT w.user_id, v.channel_id
            FROM watch_events w
            JOIN videos v ON v.video_id = w.video_id
            WHERE v.channel_id IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM subscriptions s
                              WHERE s.user_id = w.user_id
                                AND s.channel_id = v.channel_id)
              AND NOT EXISTS (SELECT 1 FROM channels c
                              WHERE c.channel_id = v.channel_id AND c.expanded)
            GROUP BY w.user_id, v.channel_id
            HAVING count(*) >= %s
        ) per_user
        """,
        (settings.expand_min_watches,),
    ).fetchall()
    for row in recurring:
        conn.execute(
            "UPDATE channels SET expanded = TRUE WHERE channel_id = %s",
            (row["channel_id"],),
        )
        added_recurring += 1
    conn.commit()

    # --- source 1: featured channels via channelSections (metered, 1 unit
    # per subscribed channel — the API takes one channelId per call) -------
    if settings.youtube_api_key:
        subscribed = [
            r["channel_id"]
            for r in conn.execute(
                # random rotation: with more subscriptions than the sample
                # size, a fixed ordering would starve the tail forever
                """
                SELECT channel_id
                FROM (SELECT DISTINCT channel_id FROM subscriptions) sub
                ORDER BY random() LIMIT %s
                """,
                (settings.channel_graph_sample,),
            ).fetchall()
        ]
        # client construction can fail (bad key, import issue) — it must
        # happen BEFORE any reservation so a failure can't strand one
        client = (
            youtube_api.build_public_client(settings.youtube_api_key)
            if subscribed else None
        )

        def _sleep(seconds: float) -> None:
            # keep the job lease alive across the retry/backoff envelope
            ctx.heartbeat()
            time.sleep(seconds)

        for source_channel in subscribed:
            ctx.heartbeat()
            try:
                reservation = quota.reserve(
                    conn, settings, "channel_graph", 1,
                    note=f"channelSections {source_channel}",
                )
            except quota.QuotaDenied:
                conn.rollback()
                stopped = "ledger-cap"
                break
            conn.commit()
            counter = youtube_api.ApiCallCounter()
            try:
                featured = youtube_api.featured_channels(
                    client, source_channel, counter=counter, sleep=_sleep
                )
            except youtube_api.QuotaExceededError:
                if counter.n > 1:
                    quota.settle(conn, reservation.reservation_id,
                                 actual_units=counter.n - 1,
                                 note="retries before API quota 403")
                else:
                    quota.refund(conn, reservation.reservation_id,
                                 note="API quota 403")
                conn.commit()
                stopped = "api-quota"
                break
            except Exception:
                quota.settle(conn, reservation.reservation_id,
                             actual_units=max(counter.n, 1),
                             note="failure (conservative)")
                conn.commit()
                raise
            quota.settle(conn, reservation.reservation_id,
                         actual_units=max(counter.n, 1))
            # settlement commits before the channel writes: a failure below
            # must not roll the charge back
            conn.commit()
            for channel_id in featured:
                exists = conn.execute(
                    "SELECT expanded FROM channels WHERE channel_id = %s",
                    (channel_id,),
                ).fetchone()
                already_sub = conn.execute(
                    "SELECT 1 FROM subscriptions WHERE channel_id = %s",
                    (channel_id,),
                ).fetchone()
                if already_sub or (exists and exists["expanded"]):
                    continue
                store.upsert_channel(conn, channel_id, source="expanded")
                conn.execute(
                    "UPDATE channels SET expanded = TRUE WHERE channel_id = %s",
                    (channel_id,),
                )
                added_featured += 1
            conn.commit()
    return {
        "expanded_recurring": added_recurring,
        "expanded_featured": added_featured,
        "stopped": stopped,
    }
