"""Nightly subscriptions + likes sync (FLOW-23, AC-29/AC-30).

Read-only OAuth. If the token is expired/revoked, PUBLIC ingestion (RSS,
hydration) is unaffected — only this private sync pauses, and an ops alert
prompts re-auth (AC-24). Every API page is metered through the quota
ledger under 'sync' with true-attempt settlement. The likes backfill
resumes from a persisted page token across nights (large like histories
exceed one night's cap); once complete, nightly runs early-stop after
``likes_early_stop_pages`` CONSECUTIVE pages with no new likes (the API
documents no ordering for myRating=liked), and a full rescan every
``likes_full_rescan_days`` bounds whatever the heuristic misses.

An incomplete pull (quota / transient / ledger stop) raises AFTER all
page-wise commits, so the framework retries with backoff and terminal
failures alert — a partial sync is never recorded as a success.
"""

from __future__ import annotations

from googleapiclient.errors import HttpError

from mytube import connect, quota, store, subsync, youtube_api
from mytube.scheduler import JobContext, raise_alert, resolve_alert


class SyncIncompleteError(Exception):
    pass


def nightly_sync(ctx: JobContext) -> dict:
    """Sync every user that has resolvable credentials (per-user encrypted
    web tokens; the CLI file token only in the single-user case)."""
    conn = ctx.conn
    users = conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()
    if not users:
        return {"skipped": "no-users"}
    from mytube.scheduler import LeaseLostError

    results: dict[int, dict] = {}
    for row in users:
        try:
            results[row["user_id"]] = _sync_user(ctx, row["user_id"])
        except LeaseLostError:
            raise                       # scheduler control flow: abort NOW
        except SyncIncompleteError as exc:
            # e.g. a stale page token for this user — recorded, next user runs
            results[row["user_id"]] = {"error": str(exc)[:300]}
        except Exception as exc:  # noqa: BLE001 — one user must not starve the rest
            conn.rollback()
            results[row["user_id"]] = {
                "error": f"{type(exc).__name__}: {exc}"[:300]
            }
    errors = {uid: r for uid, r in results.items() if r.get("error")}
    if errors:
        raise SyncIncompleteError(f"sync incomplete for users {sorted(errors)}: "
                                  f"{errors}")
    return {"users": results}


def _sync_user(ctx: JobContext, user_id: int) -> dict:
    conn, settings = ctx.conn, ctx.settings
    alert_subject = f"youtube-connection:{user_id}"
    creds = connect.credentials_for_user(conn, settings, user_id)
    if creds is None:
        raise_alert(
            conn, "oauth", alert_subject,
            "YouTube connection missing/expired/revoked — private sync paused; "
            "reconnect from the app (or `mytube auth login`) — public "
            "ingestion unaffected (AC-24)",
        )
        conn.commit()
        return {"skipped": "no-oauth"}
    resolve_alert(conn, "oauth", alert_subject)
    sync_state = store.get_sync_state(conn, user_id)
    conn.commit()
    client = youtube_api.build_oauth_client(creds)

    ctx.heartbeat()

    def beating_subs(pages):
        for page in pages:
            ctx.heartbeat()
            yield page

    subs_counter = youtube_api.ApiCallCounter()
    subs_result = subsync.pull_subscription_pages(
        conn,
        user_id,
        beating_subs(quota.metered_pages(
            conn, settings, "sync",
            youtube_api.iter_subscription_pages(client, counter=subs_counter),
            counter=subs_counter,
        )),
    )
    pruned = 0
    if subs_result["prunable"]:
        pruned = store.reconcile_subscriptions(conn, user_id, subs_result["all_ids"])
        conn.commit()

    ctx.heartbeat()
    backfill_done = sync_state["likes_backfill_done"]
    # a persisted token ALWAYS wins: it marks an unfinished full pass
    # (initial backfill or periodic rescan) that must continue where it
    # stopped — early-stop would strand everything past the failed page
    start_token = sync_state["likes_next_page_token"]
    rescan_due = conn.execute(
        """
        SELECT %s::timestamptz IS NULL
            OR %s::timestamptz < now() - make_interval(days => %s) AS due
        """,
        (
            sync_state["likes_last_full_scan_at"],
            sync_state["likes_last_full_scan_at"],
            settings.likes_full_rescan_days,
        ),
    ).fetchone()["due"]
    full_scan_mode = (not backfill_done) or (start_token is not None) or rescan_due
    likes_counter = youtube_api.ApiCallCounter()

    def beating(pages):
        for page in pages:
            ctx.heartbeat()
            yield page

    try:
        likes_result = subsync.pull_liked_video_pages(
            conn,
            user_id,
            beating(quota.metered_pages(
                conn, settings, "sync",
                youtube_api.iter_liked_video_pages(
                    client, page_token=start_token, counter=likes_counter
                ),
                counter=likes_counter,
            )),
            early_stop=not full_scan_mode,
            early_stop_pages=settings.likes_early_stop_pages,
        )
    except HttpError as exc:
        reasons = youtube_api._error_reasons(exc)
        if start_token and any("pagetoken" in r.lower() for r in reasons):
            # the persisted resume token went stale — clear it and restart
            # the full pass from page 1 on the retry
            store.update_likes_sync_state(conn, user_id, next_page_token=None)
            conn.commit()
            raise SyncIncompleteError(
                "stale likes page token cleared; retry restarts the full pass"
            ) from exc
        raise

    segment_dirty = likes_result["malformed"] > 0
    if likes_result["finished"]:
        # a COMPLETE pass ended this run. In multi-night full-scan mode the
        # whole pass is clean only if NO segment was dirty; an early-stop run
        # that naturally reached the last page is a whole pass by itself.
        scan_clean = not segment_dirty and not (
            full_scan_mode and sync_state["likes_scan_dirty"]
        )
        store.update_likes_sync_state(
            conn, user_id, next_page_token=None, backfill_done=True,
            full_scan_completed=scan_clean, reset_dirty=True,
        )
    elif full_scan_mode and likes_result["pages"] > 0 and likes_result["resume_token"]:
        store.update_likes_sync_state(
            conn, user_id, next_page_token=likes_result["resume_token"],
            mark_dirty=segment_dirty,
        )
    conn.commit()

    malformed_subject = f"likes-malformed:{user_id}"
    if segment_dirty:
        raise_alert(
            conn, "sync", malformed_subject,
            f"{likes_result['malformed']} malformed liked-video items skipped "
            "— those likes are missing from the taste set",
        )
    elif likes_result["finished"] and scan_clean:
        # only a scan-wide-clean COMPLETE pass proves the gap is repaired
        resolve_alert(conn, "sync", malformed_subject)

    ctx.heartbeat()
    taste_counts = store.rebuild_taste_items(conn, user_id, settings)
    conn.commit()

    result = {
        "subs": {k: subs_result[k] for k in ("total", "new", "pages", "error")},
        "subs_pruned": pruned,
        "likes": {
            k: likes_result[k]
            for k in ("total", "new", "pages", "finished", "error")
        },
        "taste": taste_counts,
    }
    errors = [
        f"{name}={r['error']}"
        for name, r in (("subs", subs_result), ("likes", likes_result))
        if r["error"]
    ]
    if errors:
        # all page-wise progress is committed; the caller aggregates and
        # raises so the framework retries with backoff instead of recording
        # an incomplete snapshot as success
        result["error"] = ", ".join(errors)
    return result
