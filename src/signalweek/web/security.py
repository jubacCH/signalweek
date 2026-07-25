"""Passphrase hashing helpers built on argon2-cffi.

A single :class:`PasswordHasher` instance is reused across calls: it is
thread-safe and cheap to keep around, and reusing it avoids re-deriving the
argon2 parameters on every request.
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

_hasher = PasswordHasher()


def hash_password(passphrase: str) -> str:
    """Return an argon2 hash for ``passphrase``."""
    return _hasher.hash(passphrase)


def verify_password(hashed: str, passphrase: str) -> bool:
    """Return ``True`` if ``passphrase`` matches ``hashed``."""
    try:
        return _hasher.verify(hashed, passphrase)
    except VerifyMismatchError:
        return False
