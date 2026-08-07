"""HTTP API for the SPA (Phase 2). All subjective data is user-scoped via
the session (AC-42); mutating routes require the CSRF token; blind cards
are stripped SERVER-side so packaging can't leak before reveal (AC-16).
"""

from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field, field_validator

from mytube import (
    auth_oidc,
    composer,
    connect,
    db,
    security,
    store,
    telemetry,
)
from mytube.config import Settings

router = APIRouter(prefix="/api")


# ------------------------------------------------------------ dependencies --


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_conn(settings: Annotated[Settings, Depends(get_settings)]):
    conn = db.connect(settings.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def current_session(
    request: Request,
    conn: Annotated[psycopg.Connection, Depends(get_conn)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    if settings.dev_fake_user:
        user_id = store.get_or_create_owner(conn)
        return {"session_id": "dev", "user_id": user_id, "csrf_token": "dev"}
    session = security.get_session(
        conn, request.cookies.get(security.SESSION_COOKIE)
    )
    if session is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return session


def require_csrf(
    request: Request,
    session: Annotated[dict, Depends(current_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    """Mutating routes: X-CSRF-Token header must match the session (AC-42)."""
    if settings.dev_fake_user:
        return session
    if not security.csrf_ok(session, request.headers.get("X-CSRF-Token")):
        raise HTTPException(status_code=403, detail="CSRF token missing/invalid")
    return session


Conn = Annotated[psycopg.Connection, Depends(get_conn)]
Sess = Annotated[dict, Depends(current_session)]
MutSess = Annotated[dict, Depends(require_csrf)]
Cfg = Annotated[Settings, Depends(get_settings)]


def _set_session_cookie(response: Response, settings: Settings, session_id: str):
    # Secure ALWAYS (§9), except exact-host localhost development — a
    # lookalike host (localhost.example.com) must fail closed
    response.set_cookie(
        security.SESSION_COOKIE,
        session_id,
        httponly=True,
        secure=not settings.is_localhost(),
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
        path="/",
    )


# -------------------------------------------------------------------- auth --


AUTHFLOW_COOKIE = "mytube_authflow"


@router.get("/auth/login")
def auth_login(conn: Conn, settings: Cfg):
    try:
        url, browser_token = auth_oidc.begin_signin(conn, settings)
    except auth_oidc.AuthError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    response = RedirectResponse(url, status_code=302)
    response.set_cookie(
        AUTHFLOW_COOKIE,
        browser_token,
        httponly=True,
        secure=not settings.is_localhost(),
        samesite="lax",
        max_age=600,
        path="/api/auth",
    )
    return response


@router.get("/auth/callback")
def auth_callback(
    request: Request, conn: Conn, settings: Cfg, state: str = "", code: str = ""
):
    try:
        result = auth_oidc.complete_signin(
            conn, settings, state=state, code=code,
            browser_token=request.cookies.get(AUTHFLOW_COOKIE),
        )
    except auth_oidc.AuthError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    # session rotation on login (DEC-25): drop the old session if present
    old = request.cookies.get(security.SESSION_COOKIE)
    if old:
        security.delete_session(conn, old)
    session = security.create_session(conn, settings, result["user_id"])
    response = RedirectResponse("/", status_code=302)
    _set_session_cookie(response, settings, session["session_id"])
    response.delete_cookie(AUTHFLOW_COOKIE, path="/api/auth")
    return response


@router.post("/auth/logout")
def auth_logout(request: Request, conn: Conn, session: MutSess, settings: Cfg):
    if session["session_id"] != "dev":
        security.delete_session(conn, session["session_id"])
    response = Response(status_code=204)
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return response


@router.get("/me")
def me(conn: Conn, session: Sess):
    user = conn.execute(
        "SELECT user_id, email, display_name FROM users WHERE user_id = %s",
        (session["user_id"],),
    ).fetchone()
    return {"user": dict(user), "csrf_token": session["csrf_token"]}


# ----------------------------------------------------------------- connect --


@router.get("/connect/start")
def connect_start(conn: Conn, session: Sess, settings: Cfg):
    try:
        url = connect.begin_connect(conn, settings, session["user_id"])
    except connect.ConnectError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url, status_code=302)


@router.get("/connect/callback")
def connect_callback(
    conn: Conn, session: Sess, settings: Cfg, state: str = "", code: str = ""
):
    """The callback browser must OWN the flow: without this, an attacker
    could start a connect flow and trick a victim into binding the victim's
    YouTube refresh token to the attacker's user."""
    try:
        result = connect.complete_connect(conn, settings, state=state, code=code)
    except connect.ConnectError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if result["user_id"] != session["user_id"]:
        # the flow was initiated by a different signed-in user
        conn.execute(
            "DELETE FROM oauth_tokens WHERE user_id = %s", (result["user_id"],)
        )
        raise HTTPException(status_code=403, detail="connect flow user mismatch")
    return RedirectResponse("/?connected=1", status_code=302)


@router.get("/connect/status")
def connect_status(conn: Conn, session: Sess, settings: Cfg):
    return connect.connection_status(conn, settings, session["user_id"])


@router.post("/connect/disconnect")
def connect_disconnect(conn: Conn, session: MutSess, settings: Cfg):
    removed = connect.disconnect(conn, settings, session["user_id"])
    return {"disconnected": removed}


# ------------------------------------------------------------------ status --


@router.get("/status")
def app_status(conn: Conn, session: Sess):
    """Bootstrap readiness (AC-33): usable once THIS user's subscriptions
    are imported AND the first hydration cycle produced fresh videos on
    those channels."""
    subs = conn.execute(
        "SELECT count(*) AS n FROM subscriptions WHERE user_id = %s",
        (session["user_id"],),
    ).fetchone()["n"]
    hydrated = conn.execute(
        """
        SELECT count(*) AS n FROM videos v
        WHERE v.metadata_state = 'fresh'
          AND EXISTS (SELECT 1 FROM subscriptions s
                      WHERE s.user_id = %s AND s.channel_id = v.channel_id)
        """,
        (session["user_id"],),
    ).fetchone()["n"]
    scored = conn.execute(
        "SELECT count(*) AS n FROM video_scores WHERE user_id = %s",
        (session["user_id"],),
    ).fetchone()["n"]
    ready = subs > 0 and hydrated > 0
    return {
        "ready": ready,
        "subscriptions": subs,
        "hydrated_videos": hydrated,
        "scored_videos": scored,
    }


@router.get("/quota")
def quota_today(conn: Conn, session: Sess, settings: Cfg):
    """AC-10: the UI shows quota so 'regenerate is free' is verifiable."""
    from mytube import quota as quota_mod

    return quota_mod.day_summary(conn, settings)


@router.get("/ops/summary")
def ops_summary(conn: Conn, session: Sess, settings: Cfg):
    """Authenticated ops view (FLOW-17) — quota, jobs, backlogs, alerts."""
    from mytube import quota as quota_mod

    # merge the registered roster in: a job that has NEVER run must show as
    # such, not be silently absent (AC-36)
    from mytube.app import job_specs
    from mytube.scheduler import job_status_summary, recent_job_errors

    runs = {r["job_name"]: dict(r) for r in job_status_summary(conn)}
    jobs = [
        runs.get(name) or {
            "job_name": name, "state": "never-run", "attempt": 0,
            "started_at": None, "finished_at": None, "heartbeat_at": None,
            "scheduled_for": None, "error": None, "last_success_at": None,
        }
        for name, _fn, _minutes in job_specs(settings)
    ]
    return {
        "quota": quota_mod.day_summary(conn, settings),
        "jobs": jobs,
        "recent_errors": [dict(r) for r in recent_job_errors(conn)],
        "backlogs": store.ops_backlogs(
            conn,
            model_id=settings.model_of_record(),
            shorts_max_seconds=settings.shorts_max_seconds,
            quota_day=quota_mod.quota_day(settings),
        ),
        "alerts": [
            dict(r)
            for r in conn.execute(
                "SELECT kind, subject, message, created_at FROM ops_alerts "
                "WHERE resolved_at IS NULL ORDER BY created_at DESC LIMIT 50"
            ).fetchall()
        ],
    }


# ----------------------------------------------------------------- edition --


def _card(sel: dict) -> dict:
    """Serialize one selection. An unrevealed blind card exposes ONLY the
    title (AC-16/DEC-21): no video_id (the thumbnail is derivable from it),
    no duration, no queue/like status — the reveal endpoint returns the
    full card and logs the reveal."""
    revealed = sel["revealed"] or sel["played"]
    if sel["blind"] and not revealed:
        return {
            "selection_id": sel["selection_id"],
            "position": sel["position"],
            "slot_class": sel["slot_class"],
            "blind": True,
            "video_id": None,
            "title": sel["title"] or sel["fallback_title"],
            "duration_seconds": None,
            "dismissed": sel["dismissed_at"] is not None,
            "replaced": sel["replaced_at"] is not None,
            "in_queue": False,
            "liked": False,
        }
    return {
        "selection_id": sel["selection_id"],
        "position": sel["position"],
        "slot_class": sel["slot_class"],
        "blind": False,
        "video_id": sel["video_id"],
        "title": sel["title"] or sel["fallback_title"],
        "duration_seconds": sel["duration_seconds"],
        "dismissed": sel["dismissed_at"] is not None,
        "replaced": sel["replaced_at"] is not None,
        "in_queue": sel["in_queue"],
        "liked": sel["liked"],
        "channel_id": sel["channel_id"],
        "channel_title": sel["channel_title"],
        "thumbnail_url": sel["thumbnail_url"],
        "view_count": sel["view_count"],
        "published_at": str(sel["published_at"]) if sel["published_at"] else None,
    }


def _edition_payload(conn, settings: Settings, user_id: int, edition: dict) -> dict:
    cards = conn.execute(
        """
        SELECT s.selection_id, s.position, s.slot_class, s.blind, s.video_id,
               s.dismissed_at, s.replaced_at,
               v.title, v.duration_seconds, v.channel_id, v.thumbnail_url,
               v.view_count, v.published_at,
               c.title AS channel_title,
               we.title_snapshot AS fallback_title,
               EXISTS (SELECT 1 FROM interactions x
                       WHERE x.selection_id = s.selection_id
                         AND x.kind = 'blind_reveal') AS revealed,
               EXISTS (SELECT 1 FROM interactions x
                       WHERE x.selection_id = s.selection_id
                         AND x.kind = 'play') AS played,
               EXISTS (SELECT 1 FROM watch_later q
                       WHERE q.user_id = s.user_id
                         AND q.video_id = s.video_id) AS in_queue,
               EXISTS (SELECT 1 FROM like_events l
                       WHERE l.user_id = s.user_id
                         AND l.video_id = s.video_id) AS liked
        FROM selections s
        JOIN videos v ON v.video_id = s.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        LEFT JOIN LATERAL (
            SELECT title_snapshot FROM watch_events w
            WHERE w.video_id = s.video_id AND w.user_id = s.user_id
            ORDER BY watched_at DESC LIMIT 1
        ) we ON TRUE
        WHERE s.edition_id = %s AND s.replaced_at IS NULL
          AND s.dismissed_at IS NULL
        ORDER BY s.position
        """,
        (edition["edition_id"],),
    ).fetchall()
    return {
        "edition_id": edition["edition_id"],
        "local_date": str(edition["local_date"]),
        "generation_no": edition["generation_no"],
        "note": edition["note"],
        "cards": [_card(dict(c)) for c in cards],
    }


@router.post("/selections/{selection_id}/reveal")
def reveal_selection(selection_id: int, conn: Conn, session: MutSess, settings: Cfg):
    """Log the blind reveal (AC-16) and return the FULL card — the only way
    packaging reaches the client for a blind pick. Blind-only; idempotent
    (a second reveal doesn't duplicate the log)."""
    sel = conn.execute(
        "SELECT edition_id, video_id, blind, dismissed_at FROM selections "
        "WHERE selection_id = %s AND user_id = %s FOR UPDATE",
        (selection_id, session["user_id"]),
    ).fetchone()
    if sel is None:
        raise HTTPException(status_code=404, detail="unknown selection")
    if not sel["blind"]:
        raise HTTPException(status_code=409, detail="selection is not blind")
    if sel["dismissed_at"] is not None:
        # the blind outcome was already 'dismiss' — mutually exclusive
        raise HTTPException(status_code=409, detail="selection was dismissed")
    conn.execute(
        """
        INSERT INTO interactions (user_id, video_id, selection_id, kind)
        VALUES (%s, %s, %s, 'blind_reveal')
        ON CONFLICT (selection_id) WHERE kind = 'blind_reveal' DO NOTHING
        """,
        (session["user_id"], sel["video_id"], selection_id),
    )
    edition = conn.execute(
        "SELECT * FROM editions WHERE edition_id = %s", (sel["edition_id"],)
    ).fetchone()
    payload = _edition_payload(conn, settings, session["user_id"], edition)
    card = next(
        (c for c in payload["cards"] if c["selection_id"] == selection_id), None
    )
    if card is None:
        # dismissed/replaced since serving — nothing displayable to reveal
        raise HTTPException(status_code=409, detail="selection no longer displayed")
    return {"card": card}


@router.post("/selections/{selection_id}/blind-dismiss")
def blind_dismiss(selection_id: int, conn: Conn, session: MutSess):
    """Dismiss a blind card WITHOUT revealing it (DEC-21: play or dismiss
    are the blind outcomes). The video identity never reaches the client."""
    sel = conn.execute(
        "SELECT video_id, blind FROM selections "
        "WHERE selection_id = %s AND user_id = %s FOR UPDATE",
        (selection_id, session["user_id"]),
    ).fetchone()
    if sel is None:
        raise HTTPException(status_code=404, detail="unknown selection")
    if not sel["blind"]:
        raise HTTPException(status_code=409, detail="selection is not blind")
    revealed = conn.execute(
        "SELECT 1 FROM interactions WHERE selection_id = %s "
        "AND kind = 'blind_reveal'",
        (selection_id,),
    ).fetchone()
    if revealed:
        # the blind outcome was already 'reveal' — mutually exclusive
        raise HTTPException(status_code=409, detail="selection was revealed")
    cur = conn.execute(
        "UPDATE selections SET dismissed_at = now() "
        "WHERE selection_id = %s AND dismissed_at IS NULL",
        (selection_id,),
    )
    if cur.rowcount:                     # idempotent: log the outcome once
        conn.execute(
            """
            INSERT INTO interactions (user_id, video_id, selection_id, kind)
            VALUES (%s, %s, %s, 'dismiss')
            """,
            (session["user_id"], sel["video_id"], selection_id),
        )
    return {"ok": True}


def _compose_due(conn, settings: Settings, user_id: int, edition: dict | None) -> bool:
    """§8 compose-on-demand: no edition at all, or the serving edition is
    from a previous local date and the local compose hour has passed."""
    if edition is None:
        return True
    us = composer.get_user_settings(conn, user_id)
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    local_now = datetime.now(UTC).astimezone(ZoneInfo(us["timezone"]))
    return (
        str(edition["local_date"]) < local_now.date().isoformat()
        and local_now.hour >= us["compose_hour"]
    )


@router.get("/edition")
def get_edition(conn: Conn, session: Sess, settings: Cfg):
    edition = composer.serving_edition(conn, session["user_id"])
    if _compose_due(conn, settings, session["user_id"], edition):
        # compose-on-demand catch-up (§8: a missed compose is caught on
        # first request); idempotent per day via the baseline unique index
        try:
            composer.compose_edition(
                conn, settings, session["user_id"], automatic=True
            )
            edition = composer.serving_edition(conn, session["user_id"])
        except composer.ComposeError as exc:
            # roll the failed compose back NOW — the request dependency
            # commits on success, and a persisted half-composed automatic
            # edition would poison the baseline index for the whole day
            conn.rollback()
            if edition is None:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            # keep serving the previous edition (AC-41)
    return _edition_payload(conn, settings, session["user_id"], edition)


@router.get("/edition/go-deeper/preview")
def go_deeper_preview(conn: Conn, session: Sess, settings: Cfg):
    """AC-11: the UI shows the cost BEFORE the user confirms."""
    from mytube import quota as quota_mod
    from mytube.discovery import SEARCH_COST_UNITS

    summary = quota_mod.day_summary(conn, settings)
    cat = summary["categories"]["go_deeper"]
    # same liveness predicate as the allocator: a seed whose directive died
    # must not be advertised as an available search
    have_queries = conn.execute(
        """
        SELECT count(*) AS n
        FROM search_queries q
        LEFT JOIN steering_directives d ON d.directive_id = q.directive_id
        WHERE q.user_id = %s AND q.active
          AND (q.directive_id IS NULL
               OR (d.expires_at > now() AND d.cancelled_at IS NULL))
        """,
        (session["user_id"],),
    ).fetchone()["n"]
    # never promise more searches than there are arms to run them
    planned = min(settings.go_deeper_searches, have_queries)
    return {
        "searches": planned,
        "estimated_units": planned * SEARCH_COST_UNITS,
        "category_used": cat["reserved"] + cat["charged"],
        "category_cap": cat["hard_cap"],
        "queries_available": have_queries,
    }


@router.post("/edition/go-deeper")
def go_deeper(conn: Conn, session: MutSess, settings: Cfg):
    """AC-11: spend visible quota on fresh bandit searches, hydrate the new
    finds, and report exact units after. Scoring lands with the next scorer
    tick; a regenerate then pulls the new discoveries in."""
    from mytube import discovery, hydration, youtube_api
    from mytube import quota as quota_mod

    if not settings.youtube_api_key:
        raise HTTPException(status_code=409, detail="YOUTUBE_API_KEY not configured")
    client = youtube_api.build_public_client(settings.youtube_api_key)
    result = discovery.run_searches(
        conn, settings, client, session["user_id"],
        settings.go_deeper_searches, category="go_deeper",
    )
    hydrate_result = None
    if result["new_video_ids"]:
        # hydrate EXACTLY the fresh finds — the oldest-first stub queue would
        # pick unrelated backlog first and break the "hydrated now" promise
        try:
            hydrate_result = hydration.hydrate_ids(
                conn, settings, client, result["new_video_ids"]
            )
        except youtube_api.TransientApiError:
            hydrate_result = {"stopped": "transient"}
    summary = quota_mod.day_summary(conn, settings)
    return {
        "searches_executed": result["executed"],
        "new_candidates": result["new_candidates"],
        "units_charged": result["units_charged"]
        + (hydrate_result or {}).get("units_charged", 0),
        "stopped": result["stopped"],
        # surfaced so the UI can say "found but not yet hydrated" instead of
        # implying the new finds are ready when hydration hit a cap
        "hydration_stopped": (hydrate_result or {}).get("stopped"),
        "quota_after": summary["total"],
    }


@router.post("/edition/regenerate")
def regenerate(conn: Conn, session: MutSess, settings: Cfg):
    """AC-10: free recompose from the already-scored pool; zero API calls."""
    try:
        result = composer.compose_edition(
            conn, settings, session["user_id"], automatic=False
        )
    except composer.ComposeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    edition = composer.serving_edition(conn, session["user_id"])
    return _edition_payload(conn, settings, session["user_id"], edition) | {
        "compose": result
    }


class ImpressionsIn(BaseModel):
    selection_ids: list[int] = Field(max_length=100)
    csrf: str | None = None              # pagehide flush uses sendBeacon


@router.post("/telemetry/impressions")
def record_impressions(
    payload: ImpressionsIn, request: Request, conn: Conn, session: Sess,
    settings: Cfg,
):
    """Card entered the viewport >=1s (§5 Impression, fatigue counts these)."""
    if not settings.dev_fake_user:
        header = request.headers.get("X-CSRF-Token")
        if not (security.csrf_ok(session, header)
                or security.csrf_ok(session, payload.csrf)):
            raise HTTPException(status_code=403, detail="CSRF token missing/invalid")
    for sid in payload.selection_ids:
        conn.execute(
            """
            INSERT INTO impressions (selection_id, user_id)
            SELECT s.selection_id, s.user_id FROM selections s
            WHERE s.selection_id = %s AND s.user_id = %s
            ON CONFLICT (selection_id) DO NOTHING
            """,
            (sid, session["user_id"]),
        )
    return {"ok": True}


class InteractionIn(BaseModel):
    selection_id: int | None = None
    video_id: str
    # blind_reveal is NOT accepted here — only the dedicated reveal endpoint
    # may log it (it enforces the blind-outcome state machine)
    kind: str = Field(pattern="^(play|dismiss|queue_add|open_on_youtube)$")


@router.post("/interactions")
def record_interaction(payload: InteractionIn, conn: Conn, session: MutSess):
    if conn.execute(
        "SELECT 1 FROM videos WHERE video_id = %s", (payload.video_id,)
    ).fetchone() is None:
        raise HTTPException(status_code=404, detail="unknown video")
    if payload.selection_id is not None:
        owned = conn.execute(
            "SELECT blind, dismissed_at, replaced_at FROM selections "
            "WHERE selection_id = %s AND user_id = %s AND video_id = %s "
            "FOR UPDATE",
            (payload.selection_id, session["user_id"], payload.video_id),
        ).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="unknown selection")
        if owned["blind"]:
            # blind-outcome state machine: settled (dismissed/replaced) picks
            # accept nothing; before reveal only 'play' is valid (it IS a
            # blind outcome and implies the reveal, logged here — the unique
            # partial index dedupes); 'dismiss' must use its endpoint
            if owned["dismissed_at"] is not None or owned["replaced_at"] is not None:
                raise HTTPException(status_code=409, detail="selection settled")
            if payload.kind == "dismiss":
                raise HTTPException(
                    status_code=409,
                    detail="use the blind-dismiss endpoint for blind cards",
                )
            revealed = conn.execute(
                "SELECT 1 FROM interactions WHERE selection_id = %s "
                "AND kind = 'blind_reveal'",
                (payload.selection_id,),
            ).fetchone()
            if payload.kind == "play":
                conn.execute(
                    """
                    INSERT INTO interactions
                        (user_id, video_id, selection_id, kind)
                    VALUES (%s, %s, %s, 'blind_reveal')
                    ON CONFLICT (selection_id) WHERE kind = 'blind_reveal'
                    DO NOTHING
                    """,
                    (session["user_id"], payload.video_id, payload.selection_id),
                )
            elif not revealed:
                raise HTTPException(
                    status_code=409,
                    detail="blind outcomes are play or dismiss; reveal first",
                )
    conn.execute(
        """
        INSERT INTO interactions (user_id, video_id, selection_id, kind)
        VALUES (%s, %s, %s, %s)
        """,
        (session["user_id"], payload.video_id, payload.selection_id, payload.kind),
    )
    return {"ok": True}


def _verify_gone(conn, settings: Settings, video_id: str) -> bool:
    """Server-side confirmation before a GLOBAL tombstone (§5): one metered
    videos.list call. Unverifiable (no key / transient / ledger cap) fails
    open to NOT tombstoning — a region block or flaky client must not
    delete a shared video for everyone."""
    from mytube import quota as quota_mod
    from mytube import youtube_api

    if not settings.youtube_api_key:
        return False
    try:
        reservation = quota_mod.reserve(
            conn, settings, "hydration", 1, note=f"verify unavailable {video_id}"
        )
    except quota_mod.QuotaDenied:
        return False
    conn.commit()
    counter = youtube_api.ApiCallCounter()
    try:
        client = youtube_api.build_public_client(settings.youtube_api_key)
        batch = next(iter(
            youtube_api.iter_hydration_batches(client, [video_id], counter=counter)
        ))
    except Exception:  # noqa: BLE001 — unverifiable (incl. client build) =>
        # not confirmed; settle whatever was attempted, conservatively
        quota_mod.settle(conn, reservation.reservation_id,
                         actual_units=max(counter.n, 1),
                         note="verify failed (conservative)")
        conn.commit()
        return False
    quota_mod.settle(conn, reservation.reservation_id,
                     actual_units=max(counter.n, 1))
    conn.commit()
    if batch.found:
        # still exists upstream — refresh our metadata while we're here
        from mytube import store as store_mod

        store_mod.apply_hydration(
            conn, batch.found[0], shorts_max_seconds=settings.shorts_max_seconds
        )
        return False
    return video_id in batch.missing


@router.post("/selections/{selection_id}/unavailable")
def selection_unavailable(
    selection_id: int, conn: Conn, session: MutSess, settings: Cfg
):
    """FLOW-20: video dead at play — verify upstream, then tombstone (only
    when confirmed) + atomic card replacement."""
    sel = conn.execute(
        "SELECT video_id FROM selections WHERE selection_id = %s AND user_id = %s",
        (selection_id, session["user_id"]),
    ).fetchone()
    if sel is None:
        raise HTTPException(status_code=404, detail="unknown selection")
    confirmed = _verify_gone(conn, settings, sel["video_id"])
    result = composer.replace_selection(
        conn, settings, session["user_id"], selection_id,
        confirmed_gone=confirmed,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="unknown selection")
    return result | {"tombstoned": confirmed}


@router.post("/queue/{video_id}/unavailable")
def queue_unavailable(video_id: str, conn: Conn, session: MutSess, settings: Cfg):
    """FLOW-20 for queue plays: verify; if confirmed gone, tombstone and
    drop it from the queue."""
    in_queue = conn.execute(
        "SELECT 1 FROM watch_later WHERE user_id = %s AND video_id = %s",
        (session["user_id"], video_id),
    ).fetchone()
    if in_queue is None:
        raise HTTPException(status_code=404, detail="not in queue")
    confirmed = _verify_gone(conn, settings, video_id)
    if confirmed:
        store.mark_unavailable(conn, [video_id])
        conn.execute(
            "DELETE FROM watch_later WHERE user_id = %s AND video_id = %s",
            (session["user_id"], video_id),
        )
    return {"tombstoned": confirmed, "removed": confirmed}


# --------------------------------------------------------------- telemetry --


_MAX_SECONDS = 86_400.0                  # nothing on YouTube exceeds a day


class WatchIn(BaseModel):
    watch_session_id: str = Field(min_length=8, max_length=64)
    video_id: str = Field(min_length=5, max_length=20)
    selection_id: int | None = None
    seq: int = Field(ge=1, le=1_000_000)
    position: float = Field(default=0, ge=0, le=_MAX_SECONDS)
    ranges: list[tuple[float, float]] = Field(default=[], max_length=200)
    exposure_seconds: float = Field(default=0, ge=0, le=7 * _MAX_SECONDS)
    event: str = Field(pattern="^(heartbeat|ended|close)$")
    csrf: str | None = None              # sendBeacon can't set headers (AC-7)

    @field_validator("ranges")
    @classmethod
    def _bounded_ranges(cls, v):
        for start, end in v:
            if not (0 <= start < end <= _MAX_SECONDS):
                raise ValueError(f"range [{start}, {end}] out of bounds")
        return v


@router.post("/telemetry/watch")
def watch_update(
    payload: WatchIn, request: Request, conn: Conn, session: Sess, settings: Cfg
):
    if not settings.dev_fake_user:
        header = request.headers.get("X-CSRF-Token")
        if not (security.csrf_ok(session, header)
                or security.csrf_ok(session, payload.csrf)):
            raise HTTPException(status_code=403, detail="CSRF token missing/invalid")
    return telemetry.apply_watch_update(
        conn,
        settings,
        session["user_id"],
        watch_session_id=payload.watch_session_id,
        video_id=payload.video_id,
        seq=payload.seq,
        position=payload.position,
        ranges=payload.ranges,
        exposure_seconds=payload.exposure_seconds,
        event=payload.event,
        selection_id=payload.selection_id,
        timezone=telemetry.user_timezone(conn, session["user_id"]),
    )


# ---------------------------------------------------------------- feedback --


class FeedbackIn(BaseModel):
    video_id: str = Field(min_length=5, max_length=20)
    kind: str = Field(pattern="^(like|unlike|not_interested|block_channel|clickbait)$")
    reason: str | None = Field(default=None, max_length=500)
    selection_id: int | None = None       # ties the outcome to its Selection


@router.post("/feedback")
def feedback(payload: FeedbackIn, conn: Conn, session: MutSess, settings: Cfg):
    user_id = session["user_id"]
    vid = payload.video_id
    if conn.execute(
        "SELECT 1 FROM videos WHERE video_id = %s", (vid,)
    ).fetchone() is None:
        raise HTTPException(status_code=404, detail="unknown video")
    if payload.selection_id is not None:
        owned = conn.execute(
            "SELECT 1 FROM selections WHERE selection_id = %s AND user_id = %s "
            "AND video_id = %s",
            (payload.selection_id, user_id, vid),
        ).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="unknown selection")
    if payload.kind == "like":
        # AC-27: durable LOCAL positive; zero YouTube write calls ever
        conn.execute(
            """
            INSERT INTO feedback_events (user_id, video_id, kind)
            VALUES (%s, %s, 'like')
            """,
            (user_id, vid),
        )
        conn.execute(
            """
            INSERT INTO like_events (user_id, video_id, liked_at, source)
            VALUES (%s, %s, now(), 'local')
            ON CONFLICT (user_id, video_id, source) DO NOTHING
            """,
            (user_id, vid),
        )
        conn.execute(
            """
            INSERT INTO taste_items
                (user_id, video_id, polarity, weight, source, last_event_at)
            VALUES (%s, %s, 1, %s, 'like', now())
            ON CONFLICT (user_id, video_id, source) DO UPDATE SET
                last_event_at = now(), updated_at = now()
            """,
            (user_id, vid, settings.taste_like_weight),
        )
    elif payload.kind == "unlike":
        conn.execute(
            """
            INSERT INTO feedback_events (user_id, video_id, kind)
            VALUES (%s, %s, 'unlike')
            """,
            (user_id, vid),
        )
        conn.execute(
            "DELETE FROM like_events WHERE user_id = %s AND video_id = %s "
            "AND source = 'local'",
            (user_id, vid),
        )
        # rebuild (not blind-delete) the like taste signal: an API/Takeout
        # like may remain, and its positive weight must survive the unlike
        # of the LOCAL like without waiting for the nightly rebuild
        conn.execute(
            """
            DELETE FROM taste_items t
            WHERE t.user_id = %(uid)s AND t.video_id = %(vid)s
              AND t.source = 'like'
              AND NOT EXISTS (SELECT 1 FROM like_events l
                              WHERE l.user_id = %(uid)s
                                AND l.video_id = %(vid)s)
            """,
            {"uid": user_id, "vid": vid},
        )
    elif payload.kind == "not_interested":
        # AC-8: removed from current + future editions; durable hard negative
        conn.execute(
            """
            INSERT INTO feedback_events (user_id, video_id, kind, reason)
            VALUES (%s, %s, 'not_interested', %s)
            """,
            (user_id, vid, payload.reason),
        )
        conn.execute(
            """
            INSERT INTO taste_items
                (user_id, video_id, polarity, weight, source, last_event_at)
            VALUES (%s, %s, -1, 1.0, 'feedback', now())
            ON CONFLICT (user_id, video_id, source) DO UPDATE SET
                last_event_at = now(), updated_at = now()
            """,
            (user_id, vid),
        )
        conn.execute(
            """
            UPDATE selections s SET dismissed_at = now()
            FROM editions e
            WHERE e.edition_id = s.edition_id AND e.state = 'ready'
              AND s.user_id = %s AND s.video_id = %s AND s.dismissed_at IS NULL
            """,
            (user_id, vid),
        )
        conn.execute(
            """
            INSERT INTO interactions (user_id, video_id, selection_id, kind)
            VALUES (%s, %s, %s, 'dismiss')
            """,
            (user_id, vid, payload.selection_id),
        )
    elif payload.kind == "clickbait":
        conn.execute(
            """
            INSERT INTO feedback_events (user_id, video_id, kind, reason)
            VALUES (%s, %s, 'clickbait', %s)
            """,
            (user_id, vid, payload.reason),
        )
    elif payload.kind == "block_channel":
        channel = conn.execute(
            "SELECT channel_id FROM videos WHERE video_id = %s", (vid,)
        ).fetchone()
        if channel is None or channel["channel_id"] is None:
            raise HTTPException(status_code=404, detail="video has no channel")
        conn.execute(
            """
            INSERT INTO user_channel_prefs (user_id, channel_id, blocked, blocked_at)
            VALUES (%s, %s, TRUE, now())
            ON CONFLICT (user_id, channel_id) DO UPDATE SET
                blocked = TRUE, blocked_at = now()
            """,
            (user_id, channel["channel_id"]),
        )
        # AC-9: never appears again — also purge from the serving edition
        conn.execute(
            """
            UPDATE selections s SET dismissed_at = now()
            FROM editions e, videos v
            WHERE e.edition_id = s.edition_id AND e.state = 'ready'
              AND v.video_id = s.video_id AND v.channel_id = %s
              AND s.user_id = %s AND s.dismissed_at IS NULL
            """,
            (channel["channel_id"], user_id),
        )
    return {"ok": True}


# ------------------------------------------------------------- watch later --


@router.get("/queue")
def get_queue(conn: Conn, session: Sess):
    rows = conn.execute(
        """
        SELECT q.video_id, q.position, q.added_at,
               v.title, v.duration_seconds, v.thumbnail_url,
               c.title AS channel_title
        FROM watch_later q
        JOIN videos v ON v.video_id = q.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE q.user_id = %s
        ORDER BY q.position
        """,
        (session["user_id"],),
    ).fetchall()
    return {"items": [dict(r) for r in rows]}


class QueueIn(BaseModel):
    video_id: str = Field(min_length=5, max_length=20)
    selection_id: int | None = None


def _queue_lock(conn, user_id: int) -> None:
    import hashlib as _hashlib

    key = int.from_bytes(
        _hashlib.sha256(f"queue:{user_id}".encode()).digest()[:8], "big",
        signed=True,
    )
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (key,))


@router.post("/queue")
def queue_add(payload: QueueIn, conn: Conn, session: MutSess):
    """AC-34: duplicate add is a no-op; unknown video is a 404."""
    if conn.execute(
        "SELECT 1 FROM videos WHERE video_id = %s", (payload.video_id,)
    ).fetchone() is None:
        raise HTTPException(status_code=404, detail="unknown video")
    if payload.selection_id is not None:
        owned = conn.execute(
            "SELECT 1 FROM selections WHERE selection_id = %s AND user_id = %s "
            "AND video_id = %s",
            (payload.selection_id, session["user_id"], payload.video_id),
        ).fetchone()
        if owned is None:
            raise HTTPException(status_code=404, detail="unknown selection")
    _queue_lock(conn, session["user_id"])
    cur = conn.execute(
        """
        INSERT INTO watch_later (user_id, video_id, position)
        VALUES (%(uid)s, %(vid)s,
               COALESCE((SELECT max(position) + 1 FROM watch_later
                         WHERE user_id = %(uid)s), 0))
        ON CONFLICT (user_id, video_id) DO NOTHING
        """,
        {"uid": session["user_id"], "vid": payload.video_id},
    )
    if cur.rowcount:
        conn.execute(
            """
            INSERT INTO interactions (user_id, video_id, selection_id, kind)
            VALUES (%s, %s, %s, 'queue_add')
            """,
            (session["user_id"], payload.video_id, payload.selection_id),
        )
    return {"ok": True, "added": bool(cur.rowcount)}


@router.delete("/queue/{video_id}")
def queue_remove(video_id: str, conn: Conn, session: MutSess):
    conn.execute(
        "DELETE FROM watch_later WHERE user_id = %s AND video_id = %s",
        (session["user_id"], video_id),
    )
    return {"ok": True}


class QueueOrderIn(BaseModel):
    video_ids: list[str] = Field(max_length=500)


@router.put("/queue/order")
def queue_reorder(payload: QueueOrderIn, conn: Conn, session: MutSess):
    """The new order must be an exact permutation of the current queue."""
    _queue_lock(conn, session["user_id"])
    current = {
        r["video_id"]
        for r in conn.execute(
            "SELECT video_id FROM watch_later WHERE user_id = %s FOR UPDATE",
            (session["user_id"],),
        ).fetchall()
    }
    if set(payload.video_ids) != current or len(payload.video_ids) != len(current):
        raise HTTPException(
            status_code=422,
            detail="order must be a permutation of the current queue",
        )
    for position, vid in enumerate(payload.video_ids):
        conn.execute(
            "UPDATE watch_later SET position = %s "
            "WHERE user_id = %s AND video_id = %s",
            (position, session["user_id"], vid),
        )
    return {"ok": True}


# ---------------------------------------------------------------- settings --


class SettingsIn(BaseModel):
    timezone: str | None = None
    compose_hour: int | None = Field(default=None, ge=0, le=23)
    edition_size: int | None = Field(default=None, ge=10, le=30)
    exploration_slots: int | None = Field(default=None, ge=2, le=10)
    subscription_quota: int | None = Field(default=None, ge=0, le=30)
    discovery_quota: int | None = Field(default=None, ge=0, le=30)
    wildcard_quota: int | None = Field(default=None, ge=0, le=30)
    staleness_penalty_per_day: float | None = Field(default=None, ge=0, le=1)
    duration_fit_weight: float | None = Field(default=None, ge=0, le=1)


@router.get("/settings")
def get_settings_api(conn: Conn, session: Sess):
    row = composer.get_user_settings(conn, session["user_id"])
    return {k: v for k, v in dict(row).items() if k != "user_id"}


@router.put("/settings")
def put_settings(payload: SettingsIn, conn: Conn, session: MutSess):
    """AC-28: persisted, bounds-checked (pydantic + a cross-field check that
    exploration can't be starved), effective on the next composition."""
    current = dict(composer.get_user_settings(conn, session["user_id"]))
    updates = {k: v for k, v in payload.model_dump().items() if v is not None}
    if "timezone" in updates:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(updates["timezone"])
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="unknown timezone") from exc
    merged = {**current, **updates}
    if merged["exploration_slots"] < 2:
        raise HTTPException(
            status_code=422,
            detail="exploration minimum is 2 (the counterfactual stream "
            "must not be starved — DEC-10)",
        )
    if merged["exploration_slots"] > merged["edition_size"]:
        raise HTTPException(
            status_code=422, detail="exploration slots exceed edition size"
        )
    quota_capacity = (
        merged["subscription_quota"] + merged["discovery_quota"]
        + merged["wildcard_quota"] + merged["exploration_slots"]
    )
    if quota_capacity < merged["edition_size"]:
        raise HTTPException(
            status_code=422,
            detail=f"slot quotas ({quota_capacity}) can't fill an edition of "
            f"{merged['edition_size']} — raise a quota or shrink the edition",
        )
    conn.execute(
        """
        UPDATE user_settings SET
            timezone = %(timezone)s, compose_hour = %(compose_hour)s,
            edition_size = %(edition_size)s,
            exploration_slots = %(exploration_slots)s,
            subscription_quota = %(subscription_quota)s,
            discovery_quota = %(discovery_quota)s,
            wildcard_quota = %(wildcard_quota)s,
            staleness_penalty_per_day = %(staleness_penalty_per_day)s,
            duration_fit_weight = %(duration_fit_weight)s,
            updated_at = now()
        WHERE user_id = %(user_id)s
        """,
        {**merged, "user_id": session["user_id"]},
    )
    return {k: v for k, v in merged.items() if k not in ("user_id", "updated_at")}


# ---------------------------------------------------- steering (Phase 4) ----


class SteeringIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


@router.post("/steering")
def create_steering(body: SteeringIn, conn: Conn, session: MutSess, settings: Cfg):
    """AC-12/13: parse -> pseudo-taste terms + query seeds, atomically.
    Any LLM/embedding failure surfaces as an inline error with NOTHING
    stored (the request transaction rolls back)."""
    from mytube import steering

    try:
        return steering.create_directive(
            conn, settings, session["user_id"], body.text
        )
    except steering.SteeringError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/steering")
def list_steering(conn: Conn, session: Sess):
    """AC-12/14: active directives + the expired/cancelled history."""
    from mytube import steering

    return steering.list_directives(conn, session["user_id"])


@router.post("/steering/{directive_id}/cancel")
def cancel_steering(directive_id: int, conn: Conn, session: MutSess):
    from mytube import steering

    if not steering.cancel_directive(conn, session["user_id"], directive_id):
        raise HTTPException(status_code=404, detail="no such directive")
    return {"cancelled": True}


# ---------------------------------------------------- receipts (Phase 4) ----


@router.get("/selections/{selection_id}/receipt")
def selection_receipt(selection_id: int, conn: Conn, session: Sess, settings: Cfg):
    """AC-15: why this card. Exploration/wildcard/blind cards state their
    slot type; subscription/discovery cards name concrete kNN anchors,
    active steering influence, and any overpromise penalty (AC-37)."""
    sel = conn.execute(
        """
        SELECT s.selection_id, s.video_id, s.slot_class, s.blind,
               s.propensity, s.sampling_stratum, s.score,
               EXISTS (SELECT 1 FROM interactions x
                       WHERE x.selection_id = s.selection_id
                         AND x.kind = 'blind_reveal') AS revealed,
               EXISTS (SELECT 1 FROM interactions x
                       WHERE x.selection_id = s.selection_id
                         AND x.kind = 'play') AS played,
               v.channel_id, c.title AS channel_title
        FROM selections s
        JOIN videos v ON v.video_id = s.video_id
        LEFT JOIN channels c ON c.channel_id = v.channel_id
        WHERE s.selection_id = %s AND s.user_id = %s
        """,
        (selection_id, session["user_id"]),
    ).fetchone()
    if sel is None:
        raise HTTPException(status_code=404, detail="no such selection")
    base = {
        "selection_id": sel["selection_id"],
        "slot_class": sel["slot_class"],
        "blind": sel["blind"],
    }
    # an unrevealed blind card must not leak channel/anchor context (AC-16)
    if sel["blind"] and not (sel["revealed"] or sel["played"]):
        return base | {"explanation": "blind audition - revealed on play"}
    if sel["slot_class"] in ("exploration", "wildcard"):
        slot_text = (
            "exploration slot (uniform draw)"
            if sel["slot_class"] == "exploration"
            else "trending wildcard slot"
        )
        if sel["blind"]:
            slot_text = f"blind audition - {slot_text}"
        return base | {
            "explanation": slot_text,
            "propensity": sel["propensity"],
            "sampling_stratum": sel["sampling_stratum"],
        }
    score_row = conn.execute(
        "SELECT anchors FROM video_scores WHERE user_id = %s AND video_id = %s",
        (session["user_id"], sel["video_id"]),
    ).fetchone()
    anchors = (score_row and score_row["anchors"]) or []
    steering_refs = []
    term_ids = [
        int(str(a["id"]).split(":", 1)[1])
        for a in anchors
        if a.get("kind") == "steering" and ":" in str(a.get("id"))
    ]
    if term_ids:
        steering_refs = [
            dict(r)
            for r in conn.execute(
                """
                SELECT DISTINCT d.directive_id, d.summary
                FROM steering_terms t
                JOIN steering_directives d ON d.directive_id = t.directive_id
                WHERE t.term_id = ANY(%s) AND t.user_id = %s
                """,
                (term_ids, session["user_id"]),
            ).fetchall()
        ]
    promise = None
    if sel["channel_id"]:
        row = conn.execute(
            """
            SELECT watches, overpromise, clickbait_count
            FROM channel_promise_stats
            WHERE user_id = %s AND channel_id = %s
            """,
            (session["user_id"], sel["channel_id"]),
        ).fetchone()
        if row and row["overpromise"] > 0:
            promise = {
                "overpromise": row["overpromise"],
                "watches": row["watches"],
                "clickbait_count": row["clickbait_count"],
                "penalty_applied": round(
                    settings.overpromise_penalty_weight * row["overpromise"], 4
                ),
            }
    taste_anchors = [
        a for a in anchors
        if a.get("kind") == "taste" and (a.get("title") or "").strip()
    ]
    return base | {
        "anchors": taste_anchors,
        "steering": steering_refs,
        "overpromise": promise,
        "score": sel["score"],
        "channel_title": sel["channel_title"],
        # honest fallback when no named anchor survives (pre-anchor scores,
        # empty titles, all-below-similarity-threshold): truthful and
        # neutral — no claim about WHICH signal ranked it
        "explanation": (
            None if (taste_anchors or steering_refs)
            else "ranked by your current taste and steering signals; no "
                 "sufficiently similar named anchor was available"
        ),
    }


# ---------------------------------------------- taste dashboard (Phase 4) ----


@router.get("/dashboard/taste")
def taste_dashboard(conn: Conn, session: Sess, settings: Cfg):
    """AC-35: interest clusters, drift, directive history - computed from
    durable data only (embeddings + title snapshots survive metadata
    expiry)."""
    from datetime import UTC, datetime, timedelta

    import numpy as np

    from mytube import steering
    from mytube.scoring import normalize_rows

    user_id = session["user_id"]
    model_id = settings.model_of_record()
    items = conn.execute(
        """
        SELECT t.video_id, t.weight, t.last_event_at,
               COALESCE(v.title, (
                   SELECT w.title_snapshot FROM watch_events w
                   WHERE w.user_id = t.user_id AND w.video_id = t.video_id
                     AND w.title_snapshot IS NOT NULL
                   ORDER BY w.watched_at DESC LIMIT 1
               ), t.video_id) AS title
        FROM taste_items t
        LEFT JOIN videos v ON v.video_id = t.video_id
        WHERE t.user_id = %s AND t.polarity = 1
        ORDER BY t.weight DESC, t.last_event_at DESC NULLS LAST
        LIMIT 500
        """,
        (user_id,),
    ).fetchall()
    embeddings = store.load_embeddings(
        conn, model_id, [r["video_id"] for r in items]
    )
    clustered = [r for r in items if r["video_id"] in embeddings]
    clusters: list[dict] = []
    centroids: list = []
    other: list = []
    if clustered:
        vectors = normalize_rows(
            np.stack([embeddings[r["video_id"]] for r in clustered])
        )
        for row, vec in zip(clustered, vectors, strict=True):
            # NEAREST centroid above threshold, not first-match
            best_i, best_sim = -1, 0.55
            for i, centroid in enumerate(centroids):
                sim = float(vec @ centroid)
                if sim >= best_sim:
                    best_i, best_sim = i, sim
            if best_i >= 0:
                c = clusters[best_i]
                c["members"].append(row)
                n = len(c["members"])
                updated = (centroids[best_i] * (n - 1) + vec) / n
                norm = np.linalg.norm(updated)
                centroids[best_i] = updated / norm if norm > 0 else updated
            elif len(clusters) < 15:
                clusters.append({"members": [row]})
                centroids.append(vec)
            else:
                other.append(row)          # visible, not silently dropped
    if other:
        clusters.append({"members": other, "label": "Other"})

    now = datetime.now(UTC)
    recent_cut = now - timedelta(days=30)
    prior_cut = now - timedelta(days=60)

    def summarize(c):
        members = c["members"]
        recent = sum(
            1 for m in members
            if m["last_event_at"] and m["last_event_at"] >= recent_cut
        )
        prior = sum(
            1 for m in members
            if m["last_event_at"] and prior_cut <= m["last_event_at"] < recent_cut
        )
        return {
            "label": c.get("label") or members[0]["title"],
            "size": len(members),
            "top_titles": [m["title"] for m in members[:3]],
            "recent_30d": recent,
            "prior_30d": prior,
            "drift": recent - prior,
        }

    summaries = sorted(
        (summarize(c) for c in clusters), key=lambda s: -s["size"]
    )
    top_now = conn.execute(
        """
        SELECT s.video_id, s.score,
               COALESCE(v.title, s.video_id) AS title
        FROM video_scores s
        LEFT JOIN videos v ON v.video_id = s.video_id
        WHERE s.user_id = %(uid)s AND s.model_id = %(model)s
          -- an UNREVEALED blind card must not be unblindable by correlating
          -- this list with the edition's title-only blind entry (AC-16)
          AND NOT EXISTS (
              SELECT 1 FROM selections sel
              JOIN editions e ON e.edition_id = sel.edition_id
              WHERE sel.user_id = %(uid)s AND sel.video_id = s.video_id
                AND sel.blind AND e.state = 'ready'
                AND sel.replaced_at IS NULL
                AND NOT EXISTS (
                    SELECT 1 FROM interactions x
                    WHERE x.selection_id = sel.selection_id
                      AND x.kind IN ('blind_reveal', 'play')
                )
          )
        ORDER BY s.score DESC LIMIT 10
        """,
        {"uid": user_id, "model": model_id},
    ).fetchall()
    return {
        "model_id": model_id,
        "taste_items": len(items),
        "clustered": len(clustered),
        "clusters": summaries,
        "directives": steering.list_directives(conn, user_id),
        "ranker_beliefs": {
            "top_scored": [dict(r) for r in top_now],
            "knn_k": settings.knn_k,
            "negative_penalty_weight": settings.negative_penalty_weight,
            "steering_weight": settings.steering_weight,
            "overpromise_penalty_weight": settings.overpromise_penalty_weight,
        },
    }
