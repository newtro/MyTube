"""Atomic YouTube-quota ledger (§7).

Units are reserved in the database BEFORE each API call; scheduled jobs and
user-triggered spending contend through the same transactional counters, so
a cap can never be jointly exceeded. Ambiguous outcomes are charged
conservatively: a held reservation that outlives its TTL is converted to a
charge (never silently refunded); explicit refunds require the caller to
confirm non-execution. The quota day is YouTube's (midnight Pacific), and a
reservation settles against the day it was created (§7).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import psycopg

from mytube.config import Settings

TOTAL_CATEGORY = "__total"


class QuotaDenied(Exception):
    """The requested units would exceed a hard cap or the daily budget."""


@dataclass(frozen=True)
class Reservation:
    reservation_id: int
    quota_day: date
    category: str
    units: int


def quota_day(settings: Settings, now: datetime | None = None) -> date:
    now = now or datetime.now(UTC)
    return now.astimezone(ZoneInfo(settings.quota_timezone)).date()


def _bump_reserved(
    conn: psycopg.Connection, day: date, category: str, units: int, cap: int
) -> bool:
    """Atomically add ``units`` to (day, category).reserved iff the cap holds.

    The guarded UPDATE (or conflict-update) is a single statement, so two
    concurrent reservations serialize on the row lock and the loser sees the
    winner's total — no read-modify-write race.
    """
    row = conn.execute(
        """
        INSERT INTO quota_days (quota_day, category, reserved)
        VALUES (%(day)s, %(cat)s, %(units)s)
        ON CONFLICT (quota_day, category) DO UPDATE
            SET reserved = quota_days.reserved + %(units)s
            WHERE quota_days.reserved + quota_days.charged + %(units)s <= %(cap)s
        RETURNING reserved
        """,
        {"day": day, "cat": category, "units": units, "cap": cap},
    ).fetchone()
    if row is None:
        return False
    # the fresh-INSERT path bypasses the WHERE guard; enforce the cap here
    if row["reserved"] + 0 > cap:
        # only possible when units alone exceed the cap on first insert
        conn.execute(
            "UPDATE quota_days SET reserved = reserved - %s "
            "WHERE quota_day = %s AND category = %s",
            (units, day, category),
        )
        return False
    return True


def reserve(
    conn: psycopg.Connection,
    settings: Settings,
    category: str,
    units: int,
    *,
    note: str | None = None,
    now: datetime | None = None,
) -> Reservation:
    """Reserve ``units`` for ``category`` or raise QuotaDenied.

    Runs inside the caller's transaction; caller commits. Both the category
    hard cap and the global daily budget (total minus safety reserve) must
    admit the units.
    """
    if units <= 0:
        raise ValueError("units must be positive")
    cap = settings.quota_hard_caps.get(category)
    if cap is None:
        raise ValueError(f"unknown quota category {category!r}")
    day = quota_day(settings, now)
    global_cap = settings.quota_daily_total - settings.quota_safety_reserve

    if not _bump_reserved(conn, day, TOTAL_CATEGORY, units, global_cap):
        raise QuotaDenied(
            f"daily budget exhausted ({global_cap} schedulable units on {day})"
        )
    if not _bump_reserved(conn, day, category, units, cap):
        # roll the global bump back within the same transaction
        conn.execute(
            "UPDATE quota_days SET reserved = reserved - %s "
            "WHERE quota_day = %s AND category = %s",
            (units, day, TOTAL_CATEGORY),
        )
        raise QuotaDenied(f"category {category} hard cap {cap} reached on {day}")

    row = conn.execute(
        """
        INSERT INTO quota_reservations (quota_day, category, units, note)
        VALUES (%s, %s, %s, %s) RETURNING reservation_id
        """,
        (day, category, units, note),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO quota_ledger (quota_day, category, kind, units, reservation_id, note)
        VALUES (%s, %s, 'reserve', %s, %s, %s)
        """,
        (day, category, units, row["reservation_id"], note),
    )
    return Reservation(row["reservation_id"], day, category, units)


def _release(
    conn: psycopg.Connection, res: dict, *, charged_units: int
) -> None:
    """Move a settled reservation's units out of `reserved`, into `charged`.

    Lock order MUST match reserve (__total first, then category) or a
    concurrent reserve+settle pair can deadlock on the two counter rows.
    """
    for category in (TOTAL_CATEGORY, res["category"]):
        conn.execute(
            """
            UPDATE quota_days
            SET reserved = reserved - %s, charged = charged + %s
            WHERE quota_day = %s AND category = %s
            """,
            (res["units"], charged_units, res["quota_day"], category),
        )


def _get_held(conn: psycopg.Connection, reservation_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM quota_reservations WHERE reservation_id = %s FOR UPDATE",
        (reservation_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown reservation {reservation_id}")
    if row["state"] != "held":
        raise ValueError(
            f"reservation {reservation_id} already settled ({row['state']})"
        )
    return row


def settle(
    conn: psycopg.Connection,
    reservation_id: int,
    *,
    actual_units: int | None = None,
    note: str | None = None,
) -> None:
    """Charge a reservation for the units actually spent (default: all).

    ``actual_units`` MAY exceed the reservation — retries each execute a
    real request, and true spend must be charged even when it overruns the
    upfront estimate (the safety reserve §7 absorbs the excess). The
    audit trail records a compensating 'refund' entry for any released
    remainder so ledger entries reconcile exactly.
    """
    res = _get_held(conn, reservation_id)
    if actual_units is not None and actual_units < 0:
        raise ValueError("actual_units must be >= 0")
    charged = res["units"] if actual_units is None else actual_units
    _release(conn, res, charged_units=charged)
    conn.execute(
        """
        UPDATE quota_reservations
        SET state = 'charged', charged_units = %s, settled_at = now()
        WHERE reservation_id = %s
        """,
        (charged, reservation_id),
    )
    conn.execute(
        """
        INSERT INTO quota_ledger (quota_day, category, kind, units, reservation_id, note)
        VALUES (%s, %s, 'charge', %s, %s, %s)
        """,
        (res["quota_day"], res["category"], charged, reservation_id, note),
    )
    if charged < res["units"]:
        conn.execute(
            """
            INSERT INTO quota_ledger
                (quota_day, category, kind, units, reservation_id, note)
            VALUES (%s, %s, 'refund', %s, %s, 'released unused reservation units')
            """,
            (res["quota_day"], res["category"], res["units"] - charged, reservation_id),
        )


def refund(
    conn: psycopg.Connection, reservation_id: int, *, note: str | None = None
) -> None:
    """Release a reservation with zero charge.

    Only for CONFIRMED non-execution (§7) — e.g. QuotaDenied elsewhere
    prevented the call, or the request provably never left this process.
    """
    res = _get_held(conn, reservation_id)
    _release(conn, res, charged_units=0)
    conn.execute(
        """
        UPDATE quota_reservations
        SET state = 'refunded', charged_units = 0, settled_at = now()
        WHERE reservation_id = %s
        """,
        (reservation_id,),
    )
    conn.execute(
        """
        INSERT INTO quota_ledger (quota_day, category, kind, units, reservation_id, note)
        VALUES (%s, %s, 'refund', %s, %s, %s)
        """,
        (res["quota_day"], res["category"], res["units"], reservation_id, note),
    )


def expire_stale_reservations(
    conn: psycopg.Connection, settings: Settings
) -> int:
    """Convert overdue held reservations into conservative full charges (§7:
    ambiguous timeouts are charged, refunded only on confirmed non-execution).
    Returns the number expired."""
    rows = conn.execute(
        """
        SELECT reservation_id FROM quota_reservations
        WHERE state = 'held'
          AND created_at < now() - make_interval(mins => %s)
        ORDER BY reservation_id
        FOR UPDATE SKIP LOCKED
        """,
        (settings.quota_reservation_ttl_minutes,),
    ).fetchall()
    for row in rows:
        res = conn.execute(
            "SELECT * FROM quota_reservations WHERE reservation_id = %s",
            (row["reservation_id"],),
        ).fetchone()
        _release(conn, res, charged_units=res["units"])
        conn.execute(
            """
            UPDATE quota_reservations
            SET state = 'expired_charged', charged_units = units, settled_at = now()
            WHERE reservation_id = %s
            """,
            (res["reservation_id"],),
        )
        conn.execute(
            """
            INSERT INTO quota_ledger
                (quota_day, category, kind, units, reservation_id, note)
            VALUES (%s, %s, 'expire_charge', %s, %s, 'reservation TTL expired')
            """,
            (res["quota_day"], res["category"], res["units"], res["reservation_id"]),
        )
    return len(rows)


def metered_pages(
    conn: psycopg.Connection,
    settings: Settings,
    category: str,
    page_iter,
    *,
    counter=None,
):
    """Wrap a page iterator so every underlying API call is preceded by a
    1-unit reservation, settled per §7 against TRUE attempts (pass the same
    ApiCallCounter to the iterator — retries each execute a real request):
    - page fetched          -> charge actual attempts (>= 1)
    - API quota 403         -> the rejected final attempt costs nothing;
                               charge the retries before it, else refund
    - retries exhausted     -> conservative charge of every attempt, re-raise
    - iterator finished     -> refund the unused reservation
    Commits around each movement so the ledger survives crashes.
    """
    from mytube import youtube_api  # local import to avoid a cycle

    it = iter(page_iter)
    while True:
        reservation = reserve(conn, settings, category, 1, note=f"{category} page")
        conn.commit()
        attempts_before = counter.n if counter is not None else None
        try:
            page = next(it)
        except StopIteration:
            # only reachable for iterators without next_page_token metadata
            refund(conn, reservation.reservation_id, note="no further pages")
            conn.commit()
            return
        except youtube_api.QuotaExceededError:
            spent = counter.n - attempts_before if counter is not None else None
            if spent is not None and spent > 1:
                settle(conn, reservation.reservation_id, actual_units=spent - 1,
                       note="retries before API quota 403")
            else:
                refund(conn, reservation.reservation_id, note="API quota 403")
            conn.commit()
            raise
        except youtube_api.TransientApiError:
            spent = counter.n - attempts_before if counter is not None else None
            settle(conn, reservation.reservation_id, actual_units=spent,
                   note="transient (conservative)")
            conn.commit()
            raise
        except Exception:
            # any other failure after the reserve still settles true attempts
            # (unsettled reservations TTL-charge only 1 and under-count retries)
            spent = counter.n - attempts_before if counter is not None else None
            settle(conn, reservation.reservation_id, actual_units=spent,
                   note="unexpected failure (conservative)")
            conn.commit()
            raise
        spent = counter.n - attempts_before if counter is not None else None
        settle(conn, reservation.reservation_id, actual_units=spent)
        conn.commit()
        yield page
        # page objects that carry pagination metadata let us stop WITHOUT a
        # trailing probe — no reserve-then-refund churn, and no spurious
        # QuotaDenied when the cap is exhausted by exactly the final page
        if getattr(page, "next_page_token", "__unknown__") is None:
            return


def day_summary(
    conn: psycopg.Connection, settings: Settings, day: date | None = None
) -> dict:
    """Per-category reserved/charged vs caps for the ops view (AC-17/AC-36)."""
    day = day or quota_day(settings)
    rows = conn.execute(
        "SELECT category, reserved, charged FROM quota_days WHERE quota_day = %s",
        (day,),
    ).fetchall()
    by_cat = {r["category"]: r for r in rows}
    summary = {"quota_day": day.isoformat(), "categories": {}}
    for category, cap in settings.quota_hard_caps.items():
        row = by_cat.get(category, {"reserved": 0, "charged": 0})
        summary["categories"][category] = {
            "reserved": row["reserved"],
            "charged": row["charged"],
            "hard_cap": cap,
        }
    total = by_cat.get(TOTAL_CATEGORY, {"reserved": 0, "charged": 0})
    summary["total"] = {
        "reserved": total["reserved"],
        "charged": total["charged"],
        "budget": settings.quota_daily_total - settings.quota_safety_reserve,
        "safety_reserve": settings.quota_safety_reserve,
    }
    return summary
