"""HMAC-signed session cookies used to track the logged-in user.

The cookie value is ``<user_id>.<hex-signature>`` where the signature is a
SHA-256 HMAC of ``<user_id>`` using the process-wide secret. This is
deliberately tiny — no server-side session store, no encryption. It's enough
to identify the current user across requests for the MVP while keeping the
cookie tamper-evident.

The secret is read from ``SESSION_SECRET`` and falls back to a fixed
development value so tests and local runs work without extra configuration.
Production deployments must override the env var.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from typing import Final

from fastapi import Request, Response

SESSION_COOKIE_NAME: Final[str] = "signalweek_session"
_DEFAULT_SECRET: Final[str] = "signalweek-dev-secret-change-me"
_MAX_AGE_SECONDS: Final[int] = 60 * 60 * 24 * 30  # 30 days


def _get_secret() -> bytes:
    return os.environ.get("SESSION_SECRET", _DEFAULT_SECRET).encode("utf-8")


def _sign(payload: str) -> str:
    mac = hmac.new(_get_secret(), payload.encode("utf-8"), hashlib.sha256)
    return mac.hexdigest()


def encode_session(user_id: int) -> str:
    """Return the signed cookie value carrying ``user_id``."""
    payload = str(int(user_id))
    return f"{payload}.{_sign(payload)}"


def decode_session(token: str | None) -> int | None:
    """Return the user id from ``token``, or ``None`` if it's invalid."""
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    expected = _sign(payload)
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        return int(payload)
    except ValueError:
        return None


def set_session_cookie(response: Response, user_id: int) -> None:
    """Attach a signed session cookie identifying ``user_id`` to ``response``."""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        encode_session(user_id),
        httponly=True,
        samesite="lax",
        max_age=_MAX_AGE_SECONDS,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    """Remove the session cookie from the client."""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")


def get_current_user_id(request: Request) -> int | None:
    """Return the user id encoded in the request's session cookie, if any."""
    return decode_session(request.cookies.get(SESSION_COOKIE_NAME))
