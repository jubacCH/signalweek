"""Tests for the HMAC-signed session cookie helpers."""

from __future__ import annotations

import os

import pytest

from signalweek.web.sessions import (
    SESSION_COOKIE_NAME,
    decode_session,
    encode_session,
)


def test_encode_then_decode_roundtrips() -> None:
    token = encode_session(42)
    assert decode_session(token) == 42


def test_decode_rejects_tampered_payload() -> None:
    token = encode_session(1)
    payload, sig = token.rsplit(".", 1)
    tampered = f"{int(payload) + 1}.{sig}"
    assert decode_session(tampered) is None


def test_decode_rejects_tampered_signature() -> None:
    token = encode_session(1)
    payload, _sig = token.rsplit(".", 1)
    assert decode_session(f"{payload}.deadbeef") is None


def test_decode_rejects_missing_separator() -> None:
    assert decode_session("just-a-value") is None
    assert decode_session("") is None
    assert decode_session(None) is None


def test_signature_depends_on_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SESSION_SECRET", "alpha")
    token = encode_session(7)
    monkeypatch.setenv("SESSION_SECRET", "beta")
    assert decode_session(token) is None


def test_cookie_name_is_stable() -> None:
    # Guards against accidental renames that would silently log everyone out.
    assert SESSION_COOKIE_NAME == "signalweek_session"


def teardown_module(_module: object) -> None:
    # Keep the environment clean for subsequent test files.
    os.environ.pop("SESSION_SECRET", None)
