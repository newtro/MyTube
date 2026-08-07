"""Retention job (FLOW-15): the §5 durable-vs-expiring lifecycle.

Live candidates with expired API metadata are queued for refresh
(refresh_pending -> the hydrator re-fetches them); non-live videos get
their expiring fields purged. Durable facts, embeddings, and tombstones are
untouched (AC-22). Also sweeps stale quota reservations (conservative
charge, §7).
"""

from __future__ import annotations

from mytube import quota, security, store
from mytube.scheduler import JobContext


def apply_retention(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    ctx.heartbeat()
    counts = store.apply_retention(
        conn,
        retention_days=settings.retention_days,
        shorts_max_seconds=settings.shorts_max_seconds,
    )
    expired_reservations = quota.expire_stale_reservations(conn, settings)
    purged_sessions = security.purge_expired_sessions(conn)
    conn.commit()
    return {
        **counts,
        "expired_reservations": expired_reservations,
        "purged_sessions": purged_sessions,
    }
