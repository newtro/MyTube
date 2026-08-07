"""Trending wildcard fetch (FLOW-25): one 1-unit chart call per configured
category per day. Trending videos enter ONLY the wildcard/exploration
eligibility pool — never the subscription quota (AC-31; enforced by
first_source='trending' provenance in the composer)."""

from __future__ import annotations

import time

from mytube import quota, store, youtube_api
from mytube.scheduler import JobContext


def fetch_trending(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    if not settings.trending_categories:
        return {"skipped": "no-categories (owner picks — plan §17)"}
    if not settings.youtube_api_key:
        return {"skipped": "no-api-key"}
    client = youtube_api.build_public_client(settings.youtube_api_key)
    fetched = new = 0
    stopped = None
    for category in settings.trending_categories:
        ctx.heartbeat()
        try:
            reservation = quota.reserve(
                conn, settings, "trending", 1, note=f"chart category {category}"
            )
        except quota.QuotaDenied:
            conn.rollback()
            stopped = "ledger-cap"
            break
        conn.commit()
        counter = youtube_api.ApiCallCounter()

        def _sleep(seconds: float) -> None:
            ctx.heartbeat()              # lease survives the retry envelope
            time.sleep(seconds)

        try:
            videos = youtube_api.trending_videos(
                client, category, settings.trending_region, counter=counter,
                sleep=_sleep,
            )
        except youtube_api.QuotaExceededError:
            if counter.n > 1:
                quota.settle(conn, reservation.reservation_id,
                             actual_units=counter.n - 1,
                             note="retries before API quota 403")
            else:
                quota.refund(conn, reservation.reservation_id, note="API quota 403")
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
        # settlement commits BEFORE result persistence: a failure below must
        # not roll back the charge and leave the reservation to under-count
        conn.commit()
        for video in videos:
            fetched += 1
            if video.channel_id:
                # the stub row FKs channels — the channel must exist first
                store.upsert_channel(
                    conn, video.channel_id, title=video.channel_title,
                    source="api", api_fetched=True,
                )
            # first-touch: known videos keep their original provenance
            if store.upsert_video_stub(
                conn, video.video_id, first_source="trending",
                channel_id=video.channel_id, title=video.title,
                published_at=video.published_at,
            ):
                new += 1
            # chart results carry full metadata — hydrate in place (free)
            store.apply_hydration(
                conn, video, shorts_max_seconds=settings.shorts_max_seconds
            )
        conn.commit()
    return {"fetched": fetched, "new": new, "stopped": stopped}
