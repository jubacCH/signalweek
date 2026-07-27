"""End-to-end tests for the FastAPI web layer.

The web layer is deliberately minimal for the curated-digest pivot: a health
check and a public landing page. Every per-user surface (sign-up, log in,
sources CRUD, personal digest) has been retired.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from signalweek.web import create_app


@pytest.fixture()
def client() -> Iterator[TestClient]:
    with TestClient(create_app()) as c:
        yield c


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_landing_page_renders(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "Signalweek" in body
    # Pico.css is linked from the base template.
    assert "pico.min.css" in body


def test_landing_page_uses_curated_digest_framing(client: TestClient) -> None:
    body = client.get("/").text
    # Fixed five-category taxonomy is surfaced on the landing page.
    for category in ("Models", "Lawsuits", "Funding", "Research", "Industry moves"):
        assert category in body


def test_landing_page_has_no_user_account_ctas(client: TestClient) -> None:
    body = client.get("/").text
    # The product has no accounts — sign-up / login must not appear anywhere.
    for path in ("/signup", "/login", "/logout"):
        assert path not in body


def test_pico_css_is_served(client: TestClient) -> None:
    response = client.get("/static/pico.min.css")
    assert response.status_code == 200
    assert "Pico CSS" in response.text


@pytest.mark.parametrize(
    "path",
    [
        "/signup",
        "/login",
        "/logout",
        "/sources",
        "/digest",
        "/digest/history",
        "/digest/2026-W30",
        "/digest/2026-W30.md",
        "/api/sources",
        "/api/signals",
        "/api/digest/2026-W30",
    ],
)
def test_retired_user_surfaces_are_gone(client: TestClient, path: str) -> None:
    # The former per-user routes must return 404 — they've been removed, not
    # merely gated behind auth.
    response = client.request("GET", path)
    assert response.status_code == 404
