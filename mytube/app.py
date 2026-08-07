"""FastAPI service (§4): one uvicorn worker owns the APScheduler instance.

Run with exactly ONE worker (`uvicorn mytube.app:app --workers 1`) — the
deploy unit enforces this. Advisory locks + job leases are the backstop if
that discipline is ever violated.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Response

from mytube import db, webapi
from mytube.config import Settings, load_settings
from mytube.jobs import (
    bandit_update,
    channel_graph,
    compose,
    embed,
    hydrate,
    retention,
    rss_poll,
    score,
    search,
    sync,
    trending,
)
from mytube.scheduler import run_job

logger = logging.getLogger("mytube")

# shared between the CronTriggers and the catch-up boundary math — drift
# between the two would silently skew _last_cron_instant
TRENDING_CRON_MINUTE = 20
CHANNEL_GRAPH_CRON_MINUTE = 40


def interval_job_specs(settings: Settings) -> list[tuple[str, object, int]]:
    """(job_name, entry point, interval minutes) for the frequent jobs."""
    return [
        ("rss_poll", rss_poll.poll_due_channels, settings.interval_rss_poll),
        ("hydrate", hydrate.hydrate_pending, settings.interval_hydrate),
        ("embed", embed.embed_pending, settings.interval_embed),
        ("retention", retention.apply_retention, settings.interval_retention),
        ("score", score.score_users, settings.score_interval),
        ("compose", compose.compose_due_editions, settings.compose_check_interval),
        ("search", search.run_search_tick, settings.interval_search),
        ("bandit_update", bandit_update.update_bandit,
         settings.interval_bandit_update),
    ]


def job_specs(settings: Settings) -> list[tuple[str, object, int | None]]:
    """Every registered job (sync/trending/channel_graph run on cron)."""
    return [
        *interval_job_specs(settings),
        ("sync", sync.nightly_sync, None),
        ("trending", trending.fetch_trending, None),
        ("channel_graph", channel_graph.expand_channel_graph, None),
    ]


def _job_needs_catchup(settings: Settings, job_name: str, hours: int) -> bool:
    """True when the job's last success is older than the catch-up window
    (covers 'VM was down across the cron instant' and 'first deploy')."""
    try:
        with db.connect(settings.database_url) as conn:
            row = conn.execute(
                """
                SELECT max(finished_at) AS last FROM job_runs
                WHERE job_name = %s AND state = 'succeeded'
                """,
                (job_name,),
            ).fetchone()
            if row["last"] is None:
                return True
            return conn.execute(
                "SELECT %s < now() - make_interval(hours => %s) AS stale",
                (row["last"], hours),
            ).fetchone()["stale"]
    except Exception:                        # noqa: BLE001 — DB may not be up yet
        return False


def _sync_needs_catchup(settings: Settings) -> bool:
    return _job_needs_catchup(settings, "sync", settings.sync_catchup_hours)


def _last_cron_instant(
    now: datetime, *, hour: int, minute: int, weekday: int | None = None
) -> datetime:
    """Most recent scheduled fire time for a daily (or weekly) UTC cron.
    weekday follows APScheduler/Python convention (0=Monday)."""
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is not None:
        candidate -= timedelta(days=(candidate.weekday() - weekday) % 7)
    if candidate > now:
        candidate -= timedelta(days=7 if weekday is not None else 1)
    return candidate


def _succeeded_since(conn, job_name: str, instant: datetime) -> bool:
    row = conn.execute(
        """
        SELECT max(finished_at) AS last FROM job_runs
        WHERE job_name = %s AND state = 'succeeded'
        """,
        (job_name,),
    ).fetchone()
    return row["last"] is not None and row["last"] >= instant


def _cron_needs_catchup(
    settings: Settings, job_name: str, last_instant: datetime
) -> bool:
    """True when the most recent scheduled fire has no success after it —
    i.e. the process was down across the cron instant. Age-based windows
    miss the restart-shortly-after-the-instant case; comparing against the
    boundary itself doesn't."""
    try:
        with db.connect(settings.database_url) as conn:
            return not _succeeded_since(conn, job_name, last_instant)
    except Exception:                        # noqa: BLE001 — DB may not be up yet
        # fail-safe: no catch-up; the regular cron still fires on schedule
        return False


def _catchup_fn(job_fn, boundary: datetime):
    """Wrap a job so the catch-up run no-ops when the regular cron has
    succeeded since the boundary — the catch-up may be deliberately
    scheduled AFTER an imminent cron fire (so the two can't double-spend),
    and it must also cover the fire being silently skipped when startup
    crosses the scheduled minute between add_job and scheduler.start()."""

    def _fn(ctx):
        if _succeeded_since(ctx.conn, ctx.job_name, boundary):
            return {"skipped": "cron-already-ran"}
        return job_fn(ctx)

    return _fn


def _catchup_run_date(last_instant: datetime, cadence: timedelta) -> datetime:
    """Immediately when the next fire is comfortably far; otherwise 15 min
    AFTER the imminent fire (the wrapper then no-ops if the cron ran, and
    still catches up if startup crossing the instant suppressed it)."""
    now = datetime.now(UTC)
    next_fire = last_instant + cadence
    if next_fire - now < timedelta(hours=2):
        return next_fire + timedelta(minutes=15)
    return now


def build_scheduler(settings: Settings) -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="UTC")
    for name, fn, minutes in interval_job_specs(settings):
        scheduler.add_job(
            run_job,
            "interval",
            minutes=minutes,
            args=[settings, name, fn],
            id=name,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=None,
            jitter=30,
        )
    # nightly sync at a fixed wall-clock hour so redeploys can't postpone it
    scheduler.add_job(
        run_job,
        CronTrigger(hour=settings.sync_hour_utc, minute=10, timezone="UTC"),
        args=[settings, "sync", sync.nightly_sync],
        id="sync",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,
    )
    # resume-only retry dispatcher: a failed nightly sync retries within its
    # backoff window instead of waiting until tomorrow; never starts fresh
    scheduler.add_job(
        run_job,
        "interval",
        minutes=settings.interval_sync_retry,
        args=[settings, "sync", sync.nightly_sync],
        kwargs={"resume_only": True},
        id="sync-retry",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,
    )
    scheduler.add_job(
        run_job,
        CronTrigger(hour=settings.trending_hour_utc,
                    minute=TRENDING_CRON_MINUTE, timezone="UTC"),
        args=[settings, "trending", trending.fetch_trending],
        id="trending",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,
    )
    scheduler.add_job(
        run_job,
        CronTrigger(
            day_of_week=settings.channel_graph_weekday,
            hour=settings.channel_graph_hour_utc,
            minute=CHANNEL_GRAPH_CRON_MINUTE,
            timezone="UTC",
        ),
        args=[settings, "channel_graph", channel_graph.expand_channel_graph],
        id="channel_graph",
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,
    )
    if settings.run_scheduler and _sync_needs_catchup(settings):
        scheduler.add_job(
            run_job,
            "date",                          # once, shortly after startup
            args=[settings, "sync", sync.nightly_sync],
            id="sync-catchup",
            # unlimited grace: the scheduler starts only after this function
            # returns, and APScheduler's default 1s grace would discard a
            # date job whose run_date passed during a slow startup
            misfire_grace_time=None,
        )
    # cron jobs missed while the process was down (APScheduler can't fire
    # into the past): run once at startup when the latest scheduled instant
    # has no success after it
    now = datetime.now(UTC)
    trending_last = _last_cron_instant(
        now, hour=settings.trending_hour_utc, minute=TRENDING_CRON_MINUTE
    )
    if settings.run_scheduler and _cron_needs_catchup(
        settings, "trending", trending_last
    ):
        scheduler.add_job(
            run_job, "date",
            run_date=_catchup_run_date(trending_last, timedelta(days=1)),
            args=[settings, "trending",
                  _catchup_fn(trending.fetch_trending, trending_last)],
            id="trending-catchup",
            misfire_grace_time=None,         # survive slow startups (see above)
        )
    graph_last = _last_cron_instant(
        now, hour=settings.channel_graph_hour_utc,
        minute=CHANNEL_GRAPH_CRON_MINUTE,
        weekday=settings.channel_graph_weekday,
    )
    if settings.run_scheduler and _cron_needs_catchup(
        settings, "channel_graph", graph_last
    ):
        scheduler.add_job(
            run_job, "date",
            run_date=_catchup_run_date(graph_last, timedelta(days=7)),
            args=[settings, "channel_graph",
                  _catchup_fn(channel_graph.expand_channel_graph, graph_last)],
            id="channel-graph-catchup",
            misfire_grace_time=None,         # survive slow startups (see above)
        )
    return scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = load_settings()
    if settings.dev_fake_user and not settings.is_localhost():
        # dev_fake_user disables auth AND CSRF — it must be impossible to
        # ship that state to a non-local deployment
        raise RuntimeError(
            "DEV_FAKE_USER=true is only allowed with a localhost "
            "PUBLIC_BASE_URL — refusing to start"
        )
    app.state.settings = settings
    scheduler: BackgroundScheduler | None = None
    if settings.run_scheduler:
        scheduler = build_scheduler(settings)
        scheduler.start()
        logger.info("scheduler started with %d jobs", len(scheduler.get_jobs()))
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)


app = FastAPI(title="MyTube", lifespan=lifespan)
app.include_router(webapi.router)


@app.get("/healthz")
def healthz() -> dict:
    """Liveness only — no dependencies."""
    return {"ok": True}


@app.get("/readyz")
def readyz(response: Response) -> dict:
    """Readiness: database reachable and EVERY packaged migration applied."""
    settings: Settings = app.state.settings
    try:
        packaged = {p.name for p in db.migrations_dir().glob("*.sql")}
        with db.connect(settings.database_url) as conn:
            applied = {
                r["filename"]
                for r in conn.execute(
                    "SELECT filename FROM schema_migrations"
                ).fetchall()
            }
        missing = sorted(packaged - applied)
        if missing:
            response.status_code = 503
            return {"ok": False, "missing_migrations": missing}
        return {"ok": True, "migrations": len(applied)}
    except Exception as exc:                 # noqa: BLE001 — readiness must not raise
        # /readyz is PUBLIC: raw driver errors leak DB host/user/schema —
        # log the detail server-side, return a generic failure
        logger.warning("readyz failed: %s", exc)
        response.status_code = 503
        return {"ok": False, "error": "not ready"}


# /api/ops/summary lives in webapi (session-authenticated) — the public
# surface here is liveness/readiness only.
