"""Hydrator job (FLOW-12): stub/refresh_pending videos -> fresh metadata.

The metered loop lives in mytube.hydration (shared with the CLI so manual
runs obey the same ledger). This wrapper adds the coalescing-due id
selection, the stale-reservation sweep, and lease heartbeats.
"""

from __future__ import annotations

from mytube import hydration, quota, store, youtube_api
from mytube.scheduler import JobContext


def hydrate_pending(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    # sweep overdue reservations often (this job ticks every ~15 min) so a
    # crashed holder can't suppress quota for hours
    quota.expire_stale_reservations(conn, settings)
    conn.commit()
    ids = store.videos_needing_hydration_due(
        conn, coalesce_minutes=settings.hydrate_coalesce_minutes
    )
    if not ids:
        return {"hydrated": 0}
    client = youtube_api.build_public_client(settings.youtube_api_key)
    return hydration.hydrate_ids(
        conn, settings, client, ids, heartbeat=ctx.heartbeat
    )
