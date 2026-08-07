"""Compose job: one automatic baseline edition per (user, local_date) at the
user's configured local hour; missed runs are caught up on the next tick
(§8) or on first request (compose-on-demand in the API).
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mytube import composer
from mytube.scheduler import JobContext


def compose_due_editions(ctx: JobContext) -> dict:
    conn, settings = ctx.conn, ctx.settings
    users = conn.execute(
        "SELECT DISTINCT user_id FROM user_settings "
        "UNION SELECT DISTINCT user_id FROM subscriptions"
    ).fetchall()
    composed = skipped = failed = 0
    now = datetime.now(UTC)
    for row in users:
        ctx.heartbeat()
        user_id = row["user_id"]
        us = composer.get_user_settings(conn, user_id)
        local_now = now.astimezone(ZoneInfo(us["timezone"]))
        # due once the local compose hour has arrived; on spring-forward the
        # skipped hour simply means this fires at the first valid instant
        # after the gap, and the date-keyed baseline index makes fall-back
        # compose exactly once (§8/AC-41)
        if local_now.hour < us["compose_hour"]:
            continue
        try:
            result = composer.compose_edition(
                conn, settings, user_id, automatic=True, now=now
            )
            conn.commit()
        except composer.ComposeError:
            conn.rollback()      # empty pool etc: previous ready keeps serving
            failed += 1
            continue
        if "skipped" in result:
            skipped += 1
        else:
            composed += 1
    return {"composed": composed, "skipped": skipped, "failed": failed}
