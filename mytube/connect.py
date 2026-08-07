"""YouTube-connect web flow (FLOW-1b) + per-user credential resolution.

Separate consent from sign-in; the connected Google account MAY differ from
the sign-in account. Read-only scope forever (DEC-26). PKCE + state; token
JSON Fernet-encrypted at rest (§9). Disconnect deletes the ciphertext and
best-effort revokes upstream.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import psycopg

from mytube import oauth as cli_oauth
from mytube import security
from mytube.config import Settings

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
# youtube.readonly is the only DATA scope — read-only forever (DEC-26).
# openid/email exist solely so the UI can show WHICH account is connected
# (FLOW-1b: it may differ from the sign-in account).
SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]
# any granted scope outside this set (esp. a write scope) aborts the connect
ALLOWED_GRANTED = set(SCOPES) | set(IDENTITY_SCOPES) | {"email"}


class ConnectError(Exception):
    pass


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/connect/callback"


def begin_connect(
    conn: psycopg.Connection, settings: Settings, user_id: int
) -> str:
    if not settings.oidc_client_id:
        raise ConnectError("OIDC_CLIENT_ID is not configured")
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    conn.execute(
        """
        INSERT INTO auth_flows (state, kind, user_id, pkce_verifier)
        VALUES (%s, 'connect', %s, %s)
        """,
        (state, user_id, verifier),
    )
    params = {
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(SCOPES + IDENTITY_SCOPES),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",             # guarantees a refresh token (ASM-1)
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "include_granted_scopes": "false",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}"


def complete_connect(
    conn: psycopg.Connection, settings: Settings, *, state: str, code: str
) -> dict:
    import httpx

    flow = conn.execute(
        """
        DELETE FROM auth_flows
        WHERE state = %s AND kind = 'connect'
          AND created_at > now() - interval '10 minutes'
        RETURNING user_id, pkce_verifier
        """,
        (state,),
    ).fetchone()
    if flow is None:
        raise ConnectError("unknown or expired connect state")

    response = httpx.post(
        TOKEN_ENDPOINT,
        data={
            "code": code,
            "client_id": settings.oidc_client_id,
            "client_secret": settings.oidc_client_secret,
            "redirect_uri": redirect_uri(settings),
            "grant_type": "authorization_code",
            "code_verifier": flow["pkce_verifier"],
        },
        timeout=20.0,
    )
    if response.status_code != 200:
        raise ConnectError(f"token exchange failed: HTTP {response.status_code}")
    tokens = response.json()
    if not tokens.get("refresh_token"):
        raise ConnectError(
            "no refresh token returned — the OAuth app must be in production "
            "mode and prompt=consent (ASM-1)"
        )
    granted = set((tokens.get("scope") or "").split())
    if any(s not in granted for s in SCOPES):
        raise ConnectError("required read-only scope was not granted")
    extra = granted - ALLOWED_GRANTED
    if extra:
        # a write scope (or anything unexpected) must never be stored
        raise ConnectError(f"unexpected scopes granted: {sorted(extra)}")

    # which account got connected (FLOW-1b: the UI must ALWAYS be able to
    # show it) — from the id token issued alongside, verified; an
    # unverifiable identity aborts the connect rather than storing an
    # anonymous credential
    id_token_raw = tokens.get("id_token")
    if not id_token_raw:
        raise ConnectError("no id token returned — cannot identify the account")
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token

        claims = google_id_token.verify_oauth2_token(
            id_token_raw, google_requests.Request(), settings.oidc_client_id
        )
    except Exception as exc:  # noqa: BLE001 — provider errors become clean 400s
        raise ConnectError(f"id token verification failed: {exc}") from exc
    connected_email = (claims.get("email") or "").lower() or None
    if not connected_email:
        raise ConnectError("connected account has no email claim")

    token_record = {
        "token": tokens.get("access_token"),
        "refresh_token": tokens["refresh_token"],
        "token_uri": TOKEN_ENDPOINT,
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "scopes": SCOPES,
    }
    conn.execute(
        """
        INSERT INTO oauth_tokens (user_id, token_ciphertext, connected_email)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            token_ciphertext = EXCLUDED.token_ciphertext,
            connected_email = EXCLUDED.connected_email,
            invalid_at = NULL,               -- a fresh connect heals AC-24 state
            updated_at = now()
        """,
        (flow["user_id"], security.encrypt_token(settings, token_record),
         connected_email),
    )
    return {"user_id": flow["user_id"], "connected_email": connected_email}


def _revoke(refresh_token: str) -> None:
    try:
        import httpx

        httpx.post(REVOKE_ENDPOINT, data={"token": refresh_token}, timeout=10.0)
    except Exception:  # noqa: BLE001 — revoke is best-effort
        pass


def disconnect(conn: psycopg.Connection, settings: Settings, user_id: int) -> bool:
    """Delete the stored token (and, in the single-user case, the CLI file
    token — 'disconnect' must actually disconnect, not silently fall back);
    best-effort upstream revoke (AC-24)."""
    removed = False
    row = conn.execute(
        "DELETE FROM oauth_tokens WHERE user_id = %s RETURNING token_ciphertext",
        (user_id,),
    ).fetchone()
    if row is not None:
        removed = True
        token = security.decrypt_token(settings, row["token_ciphertext"])
        if token and token.get("refresh_token"):
            _revoke(token["refresh_token"])
    user_count = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
    if user_count <= 1 and settings.oauth_token_path.exists():
        import json as _json

        try:
            raw = _json.loads(
                settings.oauth_token_path.read_text(encoding="utf-8")
            )
            if isinstance(raw, dict) and raw.get("refresh_token"):
                _revoke(raw["refresh_token"])
        except (OSError, ValueError):
            pass
        settings.oauth_token_path.unlink(missing_ok=True)
        removed = True
    return removed


def credentials_for_user(
    conn: psycopg.Connection, settings: Settings, user_id: int
):
    """Resolve YouTube credentials for ONE user.

    Order: the user's Fernet-encrypted web token; else — ONLY when this is
    the sole user in the system (single-user reality, DEC-1) — the Phase-0/1
    CLI file token. The fallback never crosses users: with 2+ users, web
    tokens are the only source. Refreshed tokens are re-encrypted and
    persisted so each use doesn't burn a refresh round-trip.
    """
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    row = conn.execute(
        "SELECT token_ciphertext FROM oauth_tokens WHERE user_id = %s", (user_id,)
    ).fetchone()
    if row is not None:
        token = security.decrypt_token(settings, row["token_ciphertext"])
        if not token:
            return None
        creds = Credentials.from_authorized_user_info(token, SCOPES)
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                # AC-24: a revoked/expired grant must flip the UI to
                # "reconnect needed", not keep claiming connected
                conn.execute(
                    "UPDATE oauth_tokens SET invalid_at = COALESCE(invalid_at, "
                    "now()) WHERE user_id = %s",
                    (user_id,),
                )
                return None
            token["token"] = creds.token
            if creds.refresh_token:
                token["refresh_token"] = creds.refresh_token
            conn.execute(
                "UPDATE oauth_tokens SET token_ciphertext = %s, "
                "invalid_at = NULL, updated_at = now() WHERE user_id = %s",
                (security.encrypt_token(settings, token), user_id),
            )
        return creds if creds.valid else None
    user_count = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
    if user_count <= 1:
        return cli_oauth.load_credentials(settings)
    return None


def connection_status(
    conn: psycopg.Connection, settings: Settings, user_id: int
) -> dict:
    row = conn.execute(
        "SELECT connected_email, updated_at, invalid_at "
        "FROM oauth_tokens WHERE user_id = %s",
        (user_id,),
    ).fetchone()
    if row:
        if row["invalid_at"] is not None:
            # AC-24: revoked upstream — sync is dead until re-connect
            return {"connected": False, "needs_reconnect": True, "via": "web",
                    "account": row["connected_email"]}
        return {"connected": True, "via": "web",
                "account": row["connected_email"],
                "updated_at": str(row["updated_at"])}
    # the CLI file token only counts in the single-user case — mirroring
    # credentials_for_user, so a second user isn't told they're connected
    # while their sync is actually disabled
    user_count = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
    if user_count <= 1:
        cli = cli_oauth.status(settings)
        return {"connected": bool(cli.get("connected")),
                "needs_reconnect": bool(cli.get("needs_reconnect")),
                "via": "cli-token", "account": None}
    return {"connected": False, "via": None, "account": None}
