"""Google OIDC sign-in (FLOW-1a, DEC-25, AC-23).

Identity is keyed by the immutable Google ``sub``; the email allowlist is
authorization only. A non-allowlisted sign-in is denied with NOTHING
created. Issuer, audience, and nonce are all validated.
"""

from __future__ import annotations

import secrets
from urllib.parse import urlencode

import psycopg
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from mytube.config import Settings

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")


class AuthError(Exception):
    pass


def redirect_uri(settings: Settings) -> str:
    return f"{settings.public_base_url.rstrip('/')}/api/auth/callback"


def begin_signin(conn: psycopg.Connection, settings: Settings) -> tuple[str, str]:
    """Store flow state + nonce + PKCE verifier (§9: PKCE on ALL OAuth
    flows) bound to a browser token. Returns (authorization URL,
    browser_token) — the caller sets the token as a short-lived cookie and
    the callback must present it (login-CSRF / session-swap defense)."""
    import base64
    import hashlib

    if not settings.oidc_client_id:
        raise AuthError("OIDC_CLIENT_ID is not configured")
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    browser_token = secrets.token_urlsafe(24)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    conn.execute(
        "INSERT INTO auth_flows (state, kind, nonce, pkce_verifier, "
        "browser_token_hash) VALUES (%s, 'signin', %s, %s, %s)",
        (state, nonce, verifier,
         hashlib.sha256(browser_token.encode()).hexdigest()),
    )
    params = {
        "client_id": settings.oidc_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_ENDPOINT}?{urlencode(params)}", browser_token


def _exchange_code(settings: Settings, code: str, verifier: str | None = None) -> dict:
    import httpx

    data = {
        "code": code,
        "client_id": settings.oidc_client_id,
        "client_secret": settings.oidc_client_secret,
        "redirect_uri": redirect_uri(settings),
        "grant_type": "authorization_code",
    }
    if verifier:
        data["code_verifier"] = verifier
    response = httpx.post(TOKEN_ENDPOINT, data=data, timeout=20.0)
    if response.status_code != 200:
        raise AuthError(f"token exchange failed: HTTP {response.status_code}")
    return response.json()


def complete_signin(
    conn: psycopg.Connection,
    settings: Settings,
    *,
    state: str,
    code: str,
    browser_token: str | None,
    verify=None,
) -> dict:
    """Validate the callback and return {'user_id', 'email'}.

    AC-23: issuer/audience/nonce validated; the browser that initiated the
    flow must present its binding token; a non-allowlisted email is
    rejected BEFORE any row is created.
    """
    import hashlib

    flow = conn.execute(
        """
        DELETE FROM auth_flows
        WHERE state = %s AND kind = 'signin'
          AND created_at > now() - interval '10 minutes'
        RETURNING nonce, pkce_verifier, browser_token_hash
        """,
        (state,),
    ).fetchone()
    if flow is None:
        raise AuthError("unknown or expired sign-in state")
    if not browser_token or hashlib.sha256(
        browser_token.encode()
    ).hexdigest() != flow["browser_token_hash"]:
        raise AuthError("sign-in flow was not initiated by this browser")

    tokens = _exchange_code(settings, code, flow["pkce_verifier"])
    # clock_skew_in_seconds: google-auth defaults to ZERO tolerance, so a
    # host clock even one second behind Google's rejects every sign-in with
    # "Token used too early". Ordinary NTP drift is seconds; a small window
    # is the standard allowance and does not weaken the signature/aud/nonce
    # checks that actually secure the token.
    verify = verify or (
        lambda raw, aud: google_id_token.verify_oauth2_token(
            raw, google_requests.Request(), aud,
            clock_skew_in_seconds=settings.oidc_clock_skew_seconds,
        )
    )
    try:
        claims = verify(tokens.get("id_token", ""), settings.oidc_client_id)
    except AuthError:
        raise
    except Exception as exc:  # noqa: BLE001 — provider errors become clean 403s
        raise AuthError(f"id token verification failed: {exc}") from exc
    if claims.get("iss") not in ISSUERS:
        raise AuthError(f"unexpected issuer {claims.get('iss')!r}")
    if claims.get("aud") != settings.oidc_client_id:
        raise AuthError("audience mismatch")
    if claims.get("nonce") != flow["nonce"]:
        raise AuthError("nonce mismatch")
    sub = claims.get("sub")
    email = (claims.get("email") or "").lower()
    if not sub:
        raise AuthError("id token carries no sub")
    if claims.get("email_verified") is not True:
        # an unverified email must not satisfy the allowlist
        raise AuthError("email is not verified by Google")
    if email not in settings.allowed_emails:
        # authorization failure: NOTHING is created (AC-23)
        raise AuthError(f"email {email!r} is not allowlisted")

    row = conn.execute(
        "SELECT user_id FROM users WHERE google_sub = %s", (sub,)
    ).fetchone()
    if row is None and email == settings.allowed_emails[0]:
        # Adoption of the Phase-0/1 CLI 'local-owner' row (imported Takeout
        # history + taste set) is reserved for the FIRST allowlisted email —
        # the owner — so a later second user can never claim that history.
        row = conn.execute(
            """
            UPDATE users
            SET google_sub = %s, email = %s,
                display_name = COALESCE(%s, display_name)
            WHERE google_sub = 'local-owner'
            RETURNING user_id
            """,
            (sub, email, claims.get("name")),
        ).fetchone()
    if row is None:
        row = conn.execute(
            """
            INSERT INTO users (google_sub, email, display_name)
            VALUES (%s, %s, %s)
            RETURNING user_id
            """,
            (sub, email, claims.get("name")),
        ).fetchone()
    else:
        conn.execute(
            "UPDATE users SET email = %s WHERE user_id = %s",
            (email, row["user_id"]),
        )
    return {"user_id": row["user_id"], "email": email}
