"""Job execution framework (§4 scheduler discipline).

Two independent mechanisms, deliberately:

1. A Postgres **advisory lock** per job name serializes concurrent
   execution. It lives on the job's connection, so if the process dies the
   lock releases automatically.
2. A persisted **job_run lease** provides crash recovery: a ``running`` row
   whose lease expired is marked ``retryable`` and resumed from its
   checkpoint by the next tick. A live-but-hung run observes its own lease
   through ``JobContext.heartbeat()`` — once lost it must abort rather than
   write, so a successor can safely take over.

Retryable failures back off exponentially; terminal failures raise an ops
alert (FLOW-17 surfaces them).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import psycopg

from mytube import db
from mytube.config import Settings

_KEY_REDACT_RE = None


def redact_secrets(text: str) -> str:
    """Strip API keys from error text before it reaches job_runs.error /
    ops_alerts.message — googleapiclient embeds the full request URL
    (including ?key=...) in HttpError strings, and the ops endpoint would
    otherwise expose it."""
    global _KEY_REDACT_RE
    if _KEY_REDACT_RE is None:
        import re

        _KEY_REDACT_RE = re.compile(r"(key=)[^&\s\"']+")
    return _KEY_REDACT_RE.sub(r"\1REDACTED", text)


class LeaseLostError(Exception):
    """This run's lease expired (or was taken over) — abort without writing."""


def advisory_key(job_name: str) -> int:
    """Stable signed-64-bit key for pg_try_advisory_lock."""
    digest = hashlib.sha256(f"mytube:{job_name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def raise_alert(
    conn: psycopg.Connection, kind: str, subject: str, message: str
) -> None:
    """Open an ops alert; a second open alert for the same (kind, subject)
    is a no-op (UNIQUE NULLS NOT DISTINCT)."""
    conn.execute(
        """
        INSERT INTO ops_alerts (kind, subject, message)
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
        """,
        (kind, subject, message),
    )


def resolve_alert(conn: psycopg.Connection, kind: str, subject: str) -> None:
    conn.execute(
        """
        UPDATE ops_alerts SET resolved_at = now()
        WHERE kind = %s AND subject = %s AND resolved_at IS NULL
        """,
        (kind, subject),
    )


@dataclass
class JobContext:
    """Handed to job functions. The job OWNS commits on ``conn`` (commit per
    batch so progress survives crashes); heartbeat between batches."""

    conn: psycopg.Connection
    settings: Settings
    job_run_id: int
    job_name: str
    attempt: int
    checkpoint: dict[str, Any] = field(default_factory=dict)

    def heartbeat(self) -> None:
        """Extend the lease — raises LeaseLostError if it already expired.

        After a lost lease the job MUST stop writing (a successor may be
        running); the framework converts the raise into a silent abort.
        """
        # clock_timestamp(), NOT now(): now() is frozen at transaction start,
        # so repeated heartbeats inside one open transaction (e.g. during an
        # API retry envelope) would never actually advance the lease
        cur = self.conn.execute(
            """
            UPDATE job_runs
            SET heartbeat_at = clock_timestamp(),
                lease_expires_at = clock_timestamp() + make_interval(secs => %s)
            WHERE job_run_id = %s AND state = 'running'
              AND lease_expires_at > clock_timestamp()
            """,
            (self.settings.job_lease_seconds, self.job_run_id),
        )
        if cur.rowcount != 1:
            raise LeaseLostError(
                f"job {self.job_name} run {self.job_run_id}: lease lost"
            )

    def save_checkpoint(self, data: dict[str, Any]) -> None:
        """Persist resume-state; committed with the job's next commit."""
        self.checkpoint = data
        self.conn.execute(
            "UPDATE job_runs SET checkpoint = %s WHERE job_run_id = %s",
            (json.dumps(data), self.job_run_id),
        )


def _reap_expired(conn: psycopg.Connection, job_name: str) -> None:
    conn.execute(
        """
        UPDATE job_runs SET state = 'retryable'
        WHERE job_name = %s AND state = 'running' AND lease_expires_at < now()
        """,
        (job_name,),
    )


def run_job(
    settings: Settings,
    job_name: str,
    fn: Callable[[JobContext], Any],
    *,
    connect: Callable[[str], psycopg.Connection] = db.connect,
    resume_only: bool = False,
) -> str:
    """Execute one job tick. Returns an outcome string for logging/tests:
    'skipped-lock' | 'skipped-backoff' | 'skipped-no-work' | 'succeeded' |
    'retryable' | 'failed' | 'lease-lost'.

    ``resume_only``: only act when a due retryable run exists — used by the
    retry dispatcher for cron jobs (e.g. sync) so a failed nightly run
    retries within its backoff window instead of waiting a whole day, while
    never STARTING a fresh run off-schedule."""
    conn = connect(settings.database_url)
    try:
        locked = conn.execute(
            "SELECT pg_try_advisory_lock(%s) AS ok", (advisory_key(job_name),)
        ).fetchone()["ok"]
        if not locked:
            conn.rollback()
            return "skipped-lock"

        _reap_expired(conn, job_name)
        resumable = conn.execute(
            """
            SELECT job_run_id, attempt, checkpoint, scheduled_for
            FROM job_runs
            WHERE job_name = %s AND state = 'retryable'
            ORDER BY job_run_id
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (job_name,),
        ).fetchone()

        if resumable and resumable["scheduled_for"] is not None:
            due = conn.execute(
                "SELECT %s <= now() AS due", (resumable["scheduled_for"],)
            ).fetchone()["due"]
            if not due:
                conn.commit()
                return "skipped-backoff"

        if resume_only and not resumable:
            conn.commit()
            return "skipped-no-work"

        if resumable:
            attempt = resumable["attempt"] + 1
            if attempt > settings.job_max_attempts:
                conn.execute(
                    """
                    UPDATE job_runs
                    SET state = 'failed', finished_at = now(),
                        error = COALESCE(error, '') || ' [max attempts exceeded]'
                    WHERE job_run_id = %s
                    """,
                    (resumable["job_run_id"],),
                )
                raise_alert(
                    conn, "job_failure", job_name,
                    f"run {resumable['job_run_id']} failed after "
                    f"{settings.job_max_attempts} attempts",
                )
                conn.commit()
                return "failed"
            job_run_id = resumable["job_run_id"]
            checkpoint = resumable["checkpoint"] or {}
            conn.execute(
                """
                UPDATE job_runs
                SET state = 'running', attempt = %s, started_at = now(),
                    heartbeat_at = now(),
                    lease_expires_at = now() + make_interval(secs => %s)
                WHERE job_run_id = %s
                """,
                (attempt, settings.job_lease_seconds, job_run_id),
            )
        else:
            attempt = 1
            checkpoint = {}
            job_run_id = conn.execute(
                """
                INSERT INTO job_runs
                    (job_name, state, attempt, started_at, heartbeat_at,
                     lease_expires_at)
                VALUES (%s, 'running', 1, now(), now(),
                        now() + make_interval(secs => %s))
                RETURNING job_run_id
                """,
                (job_name, settings.job_lease_seconds),
            ).fetchone()["job_run_id"]
        conn.commit()

        ctx = JobContext(
            conn=conn,
            settings=settings,
            job_run_id=job_run_id,
            job_name=job_name,
            attempt=attempt,
            checkpoint=checkpoint,
        )
        try:
            fn(ctx)
        except LeaseLostError:
            # a successor may already be running — abort silently, write nothing
            conn.rollback()
            return "lease-lost"
        except Exception as exc:  # noqa: BLE001 — jobs must never kill the scheduler
            conn.rollback()
            backoff_s = settings.job_backoff_base_seconds * (2 ** (attempt - 1))
            terminal = attempt >= settings.job_max_attempts
            conn.execute(
                """
                UPDATE job_runs
                SET state = %s, error = %s, finished_at = CASE WHEN %s THEN now() END,
                    scheduled_for = CASE WHEN %s THEN NULL
                                    ELSE now() + make_interval(secs => %s) END
                WHERE job_run_id = %s AND state = 'running'
                """,
                (
                    "failed" if terminal else "retryable",
                    redact_secrets(f"{type(exc).__name__}: {exc}")[:2000],
                    terminal,
                    terminal,
                    backoff_s,
                    job_run_id,
                ),
            )
            if terminal:
                raise_alert(
                    conn, "job_failure", job_name,
                    redact_secrets(f"run {job_run_id} failed terminally: {exc}")[:500],
                )
            conn.commit()
            return "failed" if terminal else "retryable"

        conn.commit()   # any uncommitted tail work from fn
        cur = conn.execute(
            """
            UPDATE job_runs
            SET state = 'succeeded', finished_at = now()
            WHERE job_run_id = %s AND state = 'running' AND lease_expires_at > now()
            """,
            (job_run_id,),
        )
        if cur.rowcount != 1:
            # lease expired at the very end — a successor may have resumed;
            # leave the row for the reaper rather than claim success
            conn.rollback()
            return "lease-lost"
        resolve_alert(conn, "job_failure", job_name)
        conn.commit()
        return "succeeded"
    finally:
        conn.close()   # releases the advisory lock with the session


def job_status_summary(conn: psycopg.Connection) -> list[dict]:
    """Latest run per job for the ops view (AC-36) — plus the last SUCCESS
    time separately, so a currently-running/failed row doesn't erase the
    "when did this last actually work" answer."""
    return conn.execute(
        """
        SELECT DISTINCT ON (r.job_name)
            r.job_name, r.state, r.attempt, r.started_at, r.finished_at,
            r.heartbeat_at, r.scheduled_for, r.error,
            ok.last_success_at
        FROM job_runs r
        LEFT JOIN LATERAL (
            SELECT max(finished_at) AS last_success_at FROM job_runs s
            WHERE s.job_name = r.job_name AND s.state = 'succeeded'
        ) ok ON TRUE
        ORDER BY r.job_name, r.job_run_id DESC
        """
    ).fetchall()


def recent_job_errors(conn: psycopg.Connection, limit: int = 20) -> list[dict]:
    """Chronological error tail (AC-36): survives retries resolving the
    alert and later successful runs."""
    return conn.execute(
        """
        SELECT job_name, state, attempt, finished_at, error
        FROM job_runs
        WHERE error IS NOT NULL
        ORDER BY job_run_id DESC
        LIMIT %s
        """,
        (limit,),
    ).fetchall()
