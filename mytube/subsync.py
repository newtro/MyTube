"""Subscription + likes sync shared by the CLI and the nightly job (FLOW-23).

Pages are persisted and committed one at a time; pruning of dropped
subscriptions requires a complete, fully-valid snapshot. Likes import via
videos.list(myRating=liked) with provenance 'api' — read-only scope, likes
never write back to YouTube (DEC-26).
"""

from __future__ import annotations

import psycopg

from mytube import store, youtube_api
from mytube.quota import QuotaDenied
from mytube.takeout import TakeoutLike


def pull_subscription_pages(conn: psycopg.Connection, user_id: int, page_iter) -> dict:
    """Consume subscription pages, upserting + committing one page at a time.

    Returns counters plus:
    - complete: pagination finished without error
    - prunable: complete AND no malformed items — the only state in which
      reconciliation (deleting dropped subscriptions) is safe
    - error: 'quota' | 'transient' | None
    """
    total = new = pages = malformed = 0
    all_ids: list[str] = []
    complete = False
    error: str | None = None
    try:
        for page in page_iter:
            pages += 1
            new += store.upsert_subscriptions(conn, user_id, page.channels, source="api")
            total += len(page.channels)
            malformed += page.malformed_items
            all_ids.extend(ch.channel_id for ch in page.channels)
            conn.commit()
        complete = True
    except youtube_api.QuotaExceededError:
        error = "quota"
    except youtube_api.TransientApiError:
        error = "transient"
    except QuotaDenied:
        error = "ledger"                  # FLOW-18: our own cap, hard stop
    return {
        "total": total,
        "new": new,
        "pages": pages,
        "malformed": malformed,
        "all_ids": all_ids,
        "complete": complete,
        "prunable": complete and malformed == 0,
        "error": error,
    }


def pull_liked_video_pages(
    conn: psycopg.Connection,
    user_id: int,
    page_iter,
    *,
    early_stop: bool = False,
    early_stop_pages: int = 3,
) -> dict:
    """Consume LikedPage objects into like_events (provenance 'api', AC-29).

    Titles/channels ride along as video stubs so later hydration is cheap.
    Idempotent: like_events has UNIQUE (user_id, video_id, source).

    Multi-night backfill support: ``resume_token`` in the result is the
    token AFTER the last fully-processed page — persist it and start the
    next night's iterator there instead of re-reading page 1 (with tens of
    thousands of likes, restarting would burn the whole sync cap on pages
    already imported and never reach the tail). ``early_stop=True`` (set
    once the backfill finished) stops after ``early_stop_pages`` CONSECUTIVE
    pages with zero new likes — the API documents no ordering for
    myRating=liked, so a single all-known page is weak evidence; the
    periodic full rescan (sync job) bounds whatever this heuristic misses.
    """
    total = new = pages = malformed = 0
    resume_token: str | None = None
    finished = False
    error: str | None = None
    consecutive_stale_pages = 0
    try:
        for page in page_iter:
            pages += 1
            malformed += page.malformed_items
            likes = []
            for video in page.videos:
                if video.channel_id:
                    store.upsert_channel(
                        conn, video.channel_id, title=video.channel_title,
                        source="api", api_fetched=True,
                    )
                store.upsert_video_stub(
                    conn, video.video_id, first_source="likes",
                    channel_id=video.channel_id, title=video.title,
                )
                likes.append(TakeoutLike(video_id=video.video_id, liked_at=None))
            inserted = store.record_likes(conn, user_id, likes, source="api")
            new += inserted
            total += len(likes)
            resume_token = page.next_page_token
            conn.commit()
            if page.next_page_token is None:
                finished = True
                break
            consecutive_stale_pages = 0 if inserted else consecutive_stale_pages + 1
            if early_stop and consecutive_stale_pages >= early_stop_pages:
                break
    except youtube_api.QuotaExceededError:
        error = "quota"
    except youtube_api.TransientApiError:
        error = "transient"
    except QuotaDenied:
        error = "ledger"
    return {
        "total": total,
        "new": new,
        "pages": pages,
        "malformed": malformed,
        "resume_token": resume_token,
        "finished": finished,
        "error": error,
    }
