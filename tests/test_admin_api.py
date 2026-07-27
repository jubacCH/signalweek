"""Tests for the internal, token-guarded admin source API.

The admin API exists so the ai-company firm's autonomous curation loop can
add or retire sources without a code deploy. These tests exercise the token
guard, the JSON envelope, and the interaction with the ``sources`` and
``source_candidates`` tables through a FastAPI ``TestClient``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool

from signalweek.db.session import create_db_engine
from signalweek.sources import (
    source_candidates_table,
    sources_metadata,
    sources_table,
)
from signalweek.web import create_app
from signalweek.web.admin import ADMIN_TOKEN_ENV_VAR, resolve_admin_token

FAKE_TOKEN = "test-admin-token-abc123"  # noqa: S105 — fake token for tests only.


@pytest.fixture()
def engine() -> Iterator[Engine]:
    eng = create_db_engine("sqlite:///:memory:", poolclass=StaticPool)
    sources_metadata.create_all(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture()
def client(engine: Engine) -> Iterator[TestClient]:
    with TestClient(create_app(engine=engine, admin_token=FAKE_TOKEN)) as c:
        yield c


@pytest.fixture()
def unconfigured_client(engine: Engine) -> Iterator[TestClient]:
    """A client where no admin token is set — every admin route must 503."""
    with TestClient(create_app(engine=engine, admin_token=None)) as c:
        yield c


def _auth_headers(token: str = FAKE_TOKEN) -> dict[str, str]:
    return {"X-Admin-Token": token}


# ---------------------------------------------------------------------------
# Token guard
# ---------------------------------------------------------------------------


class TestTokenGuard:
    def test_missing_token_rejects_with_401(self, client: TestClient) -> None:
        response = client.get("/admin/sources")
        assert response.status_code == 401
        assert "invalid or missing admin token" in response.json()["detail"]

    def test_wrong_token_rejects_with_401(self, client: TestClient) -> None:
        response = client.get("/admin/sources", headers={"X-Admin-Token": "nope"})
        assert response.status_code == 401

    def test_correct_token_allows_access(self, client: TestClient) -> None:
        response = client.get("/admin/sources", headers=_auth_headers())
        assert response.status_code == 200

    def test_unconfigured_returns_503(self, unconfigured_client: TestClient) -> None:
        # No token configured => 503, even when the caller supplies a header.
        response = unconfigured_client.get("/admin/sources", headers=_auth_headers())
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"]

    def test_post_add_requires_token_before_body_validation(self, client: TestClient) -> None:
        # An unauthenticated caller sending garbage should get 401, not 422 —
        # we do not want to leak the payload schema to random probes.
        response = client.post("/admin/sources", json={"garbage": True})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /admin/sources
# ---------------------------------------------------------------------------


class TestAddSource:
    def test_add_new_source_returns_201(self, client: TestClient, engine: Engine) -> None:
        response = client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={
                "url": "https://example.com/feed.xml",
                "kind": "rss",
                "category_hint": "models",
                "name": "Example",
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "inserted"
        assert body["source"]["url"] == "https://example.com/feed.xml"
        assert body["source"]["kind"] == "rss"
        assert body["source"]["category_hint"] == "models"
        assert body["source"]["active"] is True
        assert body["source"]["discovered"] is False
        assert isinstance(body["source"]["id"], int)

        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).one()
        assert row.url == "https://example.com/feed.xml"

    def test_add_existing_source_upserts_and_returns_200(
        self, client: TestClient, engine: Engine
    ) -> None:
        client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={
                "url": "https://example.com/feed.xml",
                "kind": "rss",
                "category_hint": "models",
            },
        )
        response = client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={
                "url": "https://example.com/feed.xml",
                "kind": "atom",
                "category_hint": "research",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "updated"
        assert body["source"]["kind"] == "atom"
        assert body["source"]["category_hint"] == "research"

        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).one()
        assert row.kind == "atom"
        assert row.category_hint == "research"

    def test_add_reports_unchanged_when_identical(self, client: TestClient) -> None:
        payload = {
            "url": "https://example.com/feed.xml",
            "kind": "rss",
            "category_hint": "models",
        }
        client.post("/admin/sources", headers=_auth_headers(), json=payload)
        response = client.post("/admin/sources", headers=_auth_headers(), json=payload)
        assert response.status_code == 200
        assert response.json()["status"] == "unchanged"

    def test_add_rejects_unknown_kind(self, client: TestClient) -> None:
        response = client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={
                "url": "https://example.com/feed.xml",
                "kind": "podcast",
                "category_hint": "models",
            },
        )
        assert response.status_code == 400
        assert "kind must be one of" in response.json()["detail"]

    def test_add_rejects_unknown_category(self, client: TestClient) -> None:
        response = client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={
                "url": "https://example.com/feed.xml",
                "kind": "rss",
                "category_hint": "sports",
            },
        )
        assert response.status_code == 400
        assert "category_hint must be one of" in response.json()["detail"]

    def test_add_rejects_blank_url(self, client: TestClient) -> None:
        response = client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={"url": "   ", "kind": "rss", "category_hint": "models"},
        )
        # min_length=1 lets pydantic reject "   " only after strip — we do the
        # strip check ourselves, so this returns our 400.
        assert response.status_code == 400
        assert "url" in response.json()["detail"]

    def test_add_rejects_missing_fields(self, client: TestClient) -> None:
        response = client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={"url": "https://example.com/feed.xml"},
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# POST /admin/sources/{id}/disable
# ---------------------------------------------------------------------------


class TestDisableSource:
    def _add(self, client: TestClient, url: str) -> int:
        response = client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={"url": url, "kind": "rss", "category_hint": "models"},
        )
        return int(response.json()["source"]["id"])

    def test_disable_flips_active_flag(self, client: TestClient, engine: Engine) -> None:
        source_id = self._add(client, "https://example.com/feed.xml")
        response = client.post(
            f"/admin/sources/{source_id}/disable",
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "disabled"
        assert body["source"]["id"] == source_id
        assert body["source"]["active"] is False

        with engine.connect() as conn:
            row = conn.execute(select(sources_table)).one()
        assert bool(row.active) is False

    def test_disable_is_idempotent(self, client: TestClient) -> None:
        source_id = self._add(client, "https://example.com/feed.xml")
        client.post(f"/admin/sources/{source_id}/disable", headers=_auth_headers())
        response = client.post(
            f"/admin/sources/{source_id}/disable",
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "already_inactive"

    def test_disable_unknown_id_returns_404(self, client: TestClient) -> None:
        response = client.post("/admin/sources/9999/disable", headers=_auth_headers())
        assert response.status_code == 404
        assert "no source with id=9999" in response.json()["detail"]

    def test_disable_requires_token(self, client: TestClient) -> None:
        response = client.post("/admin/sources/1/disable")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /admin/sources and GET /admin/candidates
# ---------------------------------------------------------------------------


class TestListEndpoints:
    def test_list_sources_returns_registry(self, client: TestClient) -> None:
        client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={"url": "https://a.example/feed", "kind": "rss", "category_hint": "models"},
        )
        client.post(
            "/admin/sources",
            headers=_auth_headers(),
            json={"url": "https://b.example/feed", "kind": "atom", "category_hint": "research"},
        )
        response = client.get("/admin/sources", headers=_auth_headers())
        assert response.status_code == 200
        payload = response.json()
        urls = [s["url"] for s in payload["sources"]]
        assert "https://a.example/feed" in urls
        assert "https://b.example/feed" in urls

    def test_list_candidates_returns_promotion_backlog(
        self, client: TestClient, engine: Engine
    ) -> None:
        with engine.begin() as conn:
            conn.execute(
                source_candidates_table.insert().values(
                    domain="cited.example",
                    first_seen_week=date(2026, 6, 29),
                    last_seen_week=date(2026, 7, 20),
                    cite_count=5,
                    distinct_weeks_count=3,
                    promoted=False,
                )
            )
        response = client.get("/admin/candidates", headers=_auth_headers())
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["candidates"]) == 1
        candidate = payload["candidates"][0]
        assert candidate["domain"] == "cited.example"
        assert candidate["cite_count"] == 5
        assert candidate["distinct_weeks_count"] == 3
        assert candidate["promoted"] is False
        assert candidate["first_seen_week"] == "2026-06-29"
        assert candidate["last_seen_week"] == "2026-07-20"

    def test_list_candidates_requires_token(self, client: TestClient) -> None:
        response = client.get("/admin/candidates")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Token resolution helper
# ---------------------------------------------------------------------------


class TestResolveAdminToken:
    def test_explicit_takes_precedence_over_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ADMIN_TOKEN_ENV_VAR, "env-value")
        assert resolve_admin_token("explicit-value") == "explicit-value"

    def test_env_used_when_no_explicit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ADMIN_TOKEN_ENV_VAR, "env-value")
        assert resolve_admin_token(None) == "env-value"

    def test_missing_env_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ADMIN_TOKEN_ENV_VAR, raising=False)
        assert resolve_admin_token(None) is None

    def test_blank_env_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ADMIN_TOKEN_ENV_VAR, "   ")
        assert resolve_admin_token(None) is None

    def test_blank_explicit_treated_as_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ADMIN_TOKEN_ENV_VAR, raising=False)
        assert resolve_admin_token("   ") is None
