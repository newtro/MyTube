"""YouTube-connect OAuth for the Phase 0 CLI (FLOW-1b, read-only forever).

Desktop-app flow with a local redirect. Scopes are read-only and stay that
way (DEC-26 — likes are local, no write scope ever). The Google OAuth app
must be in PRODUCTION mode or refresh tokens die after 7 days (ASM-1).

Phase 2 replaces this with the web-app consent flow and encrypted-at-rest
token storage; the CLI token lives in the gitignored var/ directory with
owner-only permissions (best effort on Windows).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from mytube.config import Settings

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]


def _restrict_permissions(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 600; best effort on Windows
    except OSError:
        pass


def save_credentials(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    _restrict_permissions(token_path)


def load_credentials(settings: Settings) -> Credentials | None:
    """Load saved credentials, refreshing (and re-saving) if expired.

    A revoked/expired-beyond-refresh token returns None (the caller prompts
    for `mytube auth login` — FLOW-19/AC-24 spirit) instead of a traceback.
    """
    token_path = settings.oauth_token_path
    if not token_path.exists():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    except (ValueError, KeyError, TypeError, AttributeError):
        # corrupt/truncated/non-object token files are "not connected",
        # never a traceback
        return None
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError:
            # AC-24: a revoked grant must flip status to "reconnect needed" —
            # stamp the token file so status() stops claiming connected
            _mark_invalid(token_path)
            return None
        save_credentials(creds, token_path)
    return creds if creds.valid else None


def _mark_invalid(token_path) -> None:
    try:
        raw = json.loads(token_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and not raw.get("invalid_at"):
            from datetime import UTC, datetime

            raw["invalid_at"] = datetime.now(UTC).isoformat()
            token_path.write_text(json.dumps(raw), encoding="utf-8")
    except (OSError, json.JSONDecodeError):  # best-effort; never a traceback
        pass


def login(settings: Settings) -> Credentials:
    """Run the interactive consent flow and persist the token."""
    secrets = settings.oauth_client_secrets
    if not secrets.exists():
        raise FileNotFoundError(
            f"OAuth client secrets not found at {secrets}. Create a Desktop "
            "OAuth client in the Google Cloud console and download it there "
            "— see docs/runbooks/phase0-setup.md."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent")
    save_credentials(creds, settings.oauth_token_path)
    return creds


def status(settings: Settings) -> dict:
    """Connection status for display; never includes token material."""
    token_path = settings.oauth_token_path
    if not token_path.exists():
        return {"connected": False}
    try:
        raw = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"connected": False}
    if not isinstance(raw, dict):
        return {"connected": False, "note": "token file is corrupt"}
    if raw.get("invalid_at"):
        # a refresh failed against this token — revoked or expired grant
        return {"connected": False, "needs_reconnect": True,
                "invalid_since": raw["invalid_at"]}
    expiry = raw.get("expiry")
    expired: bool | None = None
    if expiry:
        try:
            from datetime import UTC, datetime

            expired = datetime.fromisoformat(expiry) < datetime.now(UTC)
        except ValueError:
            expired = None
    return {
        "connected": True,
        "scopes": raw.get("scopes", []),
        "has_refresh_token": bool(raw.get("refresh_token")),
        "expiry": expiry,
        "access_token_expired": expired,
        "note": "expired access tokens auto-refresh if the refresh token is valid",
    }
