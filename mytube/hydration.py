"""Metered hydration loop shared by the scheduler job and the CLI.

EVERY videos.list call — scheduled or manual — goes through the same
atomic ledger reservation (§7: scheduled jobs and user-triggered spending
contend through one transactional ledger). Settlement charges true
attempts; per-batch commits keep paid-for results on any stop.
"""

from __future__ import annotations

from collections.abc import Callable

import psycopg

from mytube import quota, store, youtube_api
from mytube.config import Settings
from mytube.youtube_api import MAX_IDS_PER_REQUEST


def hydrate_ids(
    conn: psycopg.Connection,
    settings: Settings,
    client,
    ids: list[str],
    *,
    heartbeat: Callable[[], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Hydrate ``ids`` in <=50-id metered batches. Returns counters +
    ``stopped``: None | 'ledger-cap' | 'api-quota'. TransientApiError
    propagates (conservatively charged) for the caller's retry policy."""
    hydrated = missing = shorts = calls = unknown = attempts = 0
    stopped: str | None = None
    for start in range(0, len(ids), MAX_IDS_PER_REQUEST):
        chunk = ids[start : start + MAX_IDS_PER_REQUEST]
        if heartbeat:
            heartbeat()
        try:
            reservation = quota.reserve(
                conn, settings, "hydration", 1, note=f"videos.list x{len(chunk)}"
            )
        except quota.QuotaDenied:
            conn.rollback()
            stopped = "ledger-cap"          # FLOW-18: category hard stop
            break
        conn.commit()

        counter = youtube_api.ApiCallCounter()
        try:
            batch = next(
                iter(youtube_api.iter_hydration_batches(client, chunk, counter=counter))
            )
        except youtube_api.QuotaExceededError:
            # the rejected final attempt costs nothing; retries before it did
            if counter.n > 1:
                quota.settle(conn, reservation.reservation_id,
                             actual_units=counter.n - 1,
                             note="retries before API quota 403")
                attempts += counter.n - 1
            else:
                quota.refund(conn, reservation.reservation_id, note="API quota 403")
            conn.commit()
            stopped = "api-quota"
            break
        except youtube_api.TransientApiError:
            # ambiguous: charge every attempt, then let the caller retry
            quota.settle(
                conn, reservation.reservation_id, actual_units=max(counter.n, 1),
                note="transient failure (conservative)",
            )
            conn.commit()
            raise
        except Exception:
            # ANY other post-reserve failure must still settle true attempts —
            # an unsettled reservation TTL-charges only 1 unit and would
            # under-count retries (the cap-defeat leak from review round 3)
            quota.settle(
                conn, reservation.reservation_id, actual_units=max(counter.n, 1),
                note="unexpected failure (conservative)",
            )
            conn.commit()
            raise

        # charge true attempts — a call that succeeded on retry #2 spent 2
        # units. Commit the settlement BEFORE persisting results: a
        # persistence failure must roll back only the metadata writes, never
        # the settlement (an unsettled reservation TTL-charges 1 unit and
        # would under-count the retries).
        quota.settle(conn, reservation.reservation_id, actual_units=max(counter.n, 1))
        conn.commit()
        calls += 1
        attempts += counter.n
        for video in batch.found:
            flag = store.apply_hydration(
                conn, video, shorts_max_seconds=settings.shorts_max_seconds
            )
            if flag is True:
                shorts += 1
            elif flag is None:
                unknown += 1
        store.mark_unavailable(conn, batch.missing)
        store.record_hydration_batch(
            conn,
            quota_day=reservation.quota_day,
            ids_requested=len(chunk),
            ids_found=len(batch.found),
        )
        hydrated += len(batch.found)
        missing += len(batch.missing)
        conn.commit()
        if progress:
            progress(f"  batch {calls}: {hydrated} hydrated so far")

    return {
        "hydrated": hydrated,
        "tombstoned": missing,
        "shorts": shorts,
        "unknown_duration": unknown,
        "batches": calls,
        # units actually charged to the ledger (retries included)
        "units_charged": attempts,
        "stopped": stopped,
    }
