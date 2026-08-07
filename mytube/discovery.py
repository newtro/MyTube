"""Search execution shared by the scheduled bandit job and go-deeper
(FLOW-13, AC-11/32). Every search.list call is 100 units, reserved
atomically under the given category before execution; results dedupe
against existing candidates (first-touch: only previously-unknown videos
become discoveries and can ever reward the query).
"""

from __future__ import annotations

import random
import time

import psycopg

from mytube import bandit, quota, store, youtube_api
from mytube.config import Settings

SEARCH_COST_UNITS = 100


def _beating_sleep(heartbeat):
    """Retry-backoff sleep that keeps the job lease alive: a search with
    retries can outlast the lease if the heartbeat only fires per search."""

    def _sleep(seconds: float) -> None:
        if heartbeat:
            heartbeat()
        time.sleep(seconds)

    return _sleep


def run_searches(
    conn: psycopg.Connection,
    settings: Settings,
    client,
    user_id: int,
    slots: int,
    *,
    category: str,
    rng: random.Random | None = None,
    heartbeat=None,
) -> dict:
    """Execute up to ``slots`` bandit-selected searches. Returns counters +
    'stopped' (None | 'ledger-cap' | 'api-quota'). Commits per search."""
    queries = bandit.select_queries(conn, settings, user_id, slots, rng=rng)
    executed = new_candidates = charged_units = 0
    new_video_ids: list[str] = []
    stopped: str | None = None
    for query in queries:
        if heartbeat:
            heartbeat()
        try:
            reservation = quota.reserve(
                conn, settings, category, SEARCH_COST_UNITS,
                note=f"search.list {query['query_text'][:60]!r}",
            )
        except quota.QuotaDenied:
            conn.rollback()
            stopped = "ledger-cap"          # AC-20: hard stop, no more searches
            break
        conn.commit()

        counter = youtube_api.ApiCallCounter()
        try:
            results = youtube_api.search_videos(
                client, query["query_text"], counter=counter,
                sleep=_beating_sleep(heartbeat),
            )
        except youtube_api.QuotaExceededError:
            spent = (counter.n - 1) * SEARCH_COST_UNITS
            if spent > 0:
                quota.settle(conn, reservation.reservation_id,
                             actual_units=spent,
                             note="retries before API quota 403")
                charged_units += spent       # AC-11: report the REAL spend
            else:
                quota.refund(conn, reservation.reservation_id, note="API quota 403")
            conn.commit()
            stopped = "api-quota"
            break
        except youtube_api.TransientApiError:
            quota.settle(conn, reservation.reservation_id,
                         actual_units=max(counter.n, 1) * SEARCH_COST_UNITS,
                         note="transient failure (conservative)")
            conn.commit()
            raise
        except Exception:
            # Conscious exception to "no writes after lease loss": when the
            # failure IS LeaseLostError (raised by the heartbeat inside the
            # retry sleep), this settle+commit still runs. It touches only
            # reservation rows this run owns, and recording the true retry
            # spend beats letting the TTL sweep under-charge it.
            quota.settle(conn, reservation.reservation_id,
                         actual_units=max(counter.n, 1) * SEARCH_COST_UNITS,
                         note="unexpected failure (conservative)")
            conn.commit()
            raise

        units = max(counter.n, 1) * SEARCH_COST_UNITS
        quota.settle(conn, reservation.reservation_id, actual_units=units)
        # commit the settlement BEFORE the trial/discovery writes: if those
        # fail, a rolled-back settle would leave the reservation to TTL-charge
        # only the reserved 100 units, under-counting real retry spend
        conn.commit()
        charged_units += units
        trial = conn.execute(
            """
            INSERT INTO search_trials
                (query_id, user_id, cost_units, results, speculative,
                 window_closes_at)
            VALUES (%s, %s, %s, %s, %s,
                    now() + make_interval(days => %s))
            RETURNING trial_id
            """,
            (query["query_id"], user_id, units, len(results),
             query["speculative"], settings.attribution_window_days),
        ).fetchone()
        trial_new = 0
        for result in results:
            if result.channel_id:
                store.upsert_channel(
                    conn, result.channel_id, title=result.channel_title,
                    source="api", api_fetched=True,
                )
            # first-touch: a discovery row exists ONLY for videos this trial
            # introduced to the system (AC-21/32 exclusive attribution)
            is_new = store.upsert_video_stub(
                conn, result.video_id, first_source="search",
                channel_id=result.channel_id, title=result.title,
            )
            # reach is per-sighting: THIS user's search returned it, so THIS
            # user can be served it even when the video was already known
            conn.execute(
                "INSERT INTO search_sightings (user_id, video_id) "
                "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_id, result.video_id),
            )
            if is_new:
                conn.execute(
                    "INSERT INTO search_discoveries (trial_id, video_id) "
                    "VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (trial["trial_id"], result.video_id),
                )
                trial_new += 1
                new_video_ids.append(result.video_id)
        conn.execute(
            "UPDATE search_trials SET new_candidates = %s WHERE trial_id = %s",
            (trial_new, trial["trial_id"]),
        )
        conn.execute(
            """
            UPDATE search_queries
            SET total_searches = total_searches + 1, last_searched_at = now()
            WHERE query_id = %s
            """,
            (query["query_id"],),
        )
        conn.commit()
        executed += 1
        new_candidates += trial_new
    return {
        "executed": executed,
        "new_candidates": new_candidates,
        "new_video_ids": new_video_ids,
        "units_charged": charged_units,
        "stopped": stopped,
    }
