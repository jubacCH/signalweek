"""Bearer-token helpers for the JSON API.

Tokens are 32 random bytes rendered as URL-safe base64 with a short ``sw_``
prefix so they are easy to spot in logs and reviews. Only the SHA-256 hash of
the plaintext token is stored in the database; the plaintext value is returned
to the caller exactly once at creation time.
"""

from __future__ import annotations

import hashlib
import secrets

TOKEN_PREFIX = "sw_"


def generate_token() -> str:
    """Return a fresh plaintext API token."""
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of ``token`` for database lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
