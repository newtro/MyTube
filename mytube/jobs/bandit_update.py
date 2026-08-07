"""Bandit posterior updates from telemetry (FLOW-16, AC-21) + steering
directive expiry upkeep (AC-14: an expired/cancelled directive's seeded
queries stop searching)."""

from __future__ import annotations

from mytube import bandit, steering
from mytube.scheduler import JobContext


def update_bandit(ctx: JobContext) -> dict:
    ctx.heartbeat()
    result = bandit.apply_rewards(ctx.conn, ctx.settings)
    result["steering_queries_deactivated"] = steering.deactivate_expired_queries(
        ctx.conn
    )
    ctx.conn.commit()
    return result
