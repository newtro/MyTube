"""Sessions, CSRF, and token encryption (§9, DEC-25).

Server-side sessions: HttpOnly + Secure + SameSite=Lax cookie carrying an
opaque id; session rotation on login; CSRF double-check via a per-session
token that mutating routes must echo in the X-CSRF-Token header (or, for
sendBeacon payloads that cannot set headers, a ``csrf`` body field).
OAuth tokens are Fernet-encrypted with a key that lives outside the
database; tokens and keys never appear in logs.
"""

from __future__ import annotations

import json
import secrets

import psycopg
from cryptography.fernet import Fernet, InvalidToken

from mytube.config import Settings

SESSION_COOKIE = "mytube_session"


def _fernet(settings: Settings):
    """MultiFernet over comma-separated keys: encrypt with the FIRST key,
    decrypt with any — rotation = prepend a new key, re-encrypt lazily (the
    documented §9 rotation procedure), then drop the old key."""
    from cryptography.fernet import MultiFernet

    if not settings.token_encryption_key:
        raise RuntimeError(
            "TOKEN_ENCRYPTION_KEY is not set — generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"`"
        )
    keys = [k.strip() for k in settings.token_encryption_key.split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode()) for k in keys])


def encrypt_token(settings: Settings, token: dict) -> bytes:
    return _fernet(settings).encrypt(json.dumps(token).encode())


def decrypt_token(settings: Settings, ciphertext: bytes) -> dict | None:
    try:
        return json.loads(_fernet(settings).decrypt(bytes(ciphertext)))
    except (InvalidToken, json.JSONDecodeError):
        return None


def create_session(
    conn: psycopg.Connection, settings: Settings, user_id: int
) -> dict:
    """Issue a fresh session (rotation: callers delete the old one first)."""
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    conn.execute(
        """
        INSERT INTO sessions (session_id, user_id, csrf_token, expires_at)
        VALUES (%s, %s, %s, now() + make_interval(hours => %s))
        """,
        (session_id, user_id, csrf_token, settings.session_ttl_hours),
    )
    return {"session_id": session_id, "csrf_token": csrf_token, "user_id": user_id}


def get_session(conn: psycopg.Connection, session_id: str | None) -> dict | None:
    if not session_id:
        return None
    return conn.execute(
        """
        SELECT session_id, user_id, csrf_token FROM sessions
        WHERE session_id = %s AND expires_at > now()
        """,
        (session_id,),
    ).fetchone()


def delete_session(conn: psycopg.Connection, session_id: str) -> None:
    conn.execute("DELETE FROM sessions WHERE session_id = %s", (session_id,))


def purge_expired_sessions(conn: psycopg.Connection) -> int:
    """Called by the retention job: expired sessions + abandoned auth flows."""
    n = conn.execute("DELETE FROM sessions WHERE expires_at <= now()").rowcount
    n += conn.execute(
        "DELETE FROM auth_flows WHERE created_at < now() - interval '1 hour'"
    ).rowcount
    return n


def csrf_ok(session: dict, provided: str | None) -> bool:
    return bool(provided) and secrets.compare_digest(
        session["csrf_token"], provided or ""
    )
