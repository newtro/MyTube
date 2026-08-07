"""Scheduled bandit search (FLOW-13): a few 100-unit searches per tick,
allocated by Thompson sampling with the ~20% speculative reservation."""

from __future__ import annotations

from mytube import discovery, youtube_api
from mytube.scheduler import JobContext


def run_search_tick(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    if not settings.youtube_api_key:
        return {"skipped": "no-api-key"}
    users = conn.execute(
        "SELECT DISTINCT user_id FROM search_queries WHERE active"
    ).fetchall()
    if not users:
        return {"skipped": "no-queries"}
    client = youtube_api.build_public_client(settings.youtube_api_key)
    results = {}
    for row in users:
        ctx.heartbeat()
        results[row["user_id"]] = discovery.run_searches(
            conn, settings, client, row["user_id"],
            settings.search_slots_per_tick,
            category="search", heartbeat=ctx.heartbeat,
        )
        if results[row["user_id"]]["stopped"] in ("api-quota", "ledger-cap"):
            # both stops are global-for-today: the API quota is shared, and
            # the 'search' ledger category is one pot — later users would
            # only burn reserve-then-deny round trips
            break
    return results


