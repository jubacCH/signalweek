"""Internal, token-guarded admin API for autonomous source curation.

This module exposes the source registry over HTTP so the ai-company firm's
autonomous curation loop can add or retire sources without a code deploy.
It is **not** user-facing — every endpoint requires the ``X-Admin-Token``
header to match the value configured via the ``SIGNALWEEK_ADMIN_TOKEN``
environment variable (or the ``admin_token`` argument to
:func:`signalweek.web.create_app`).

When no token is configured the router refuses every request with 503, so
misconfigured deployments cannot accidentally expose the admin surface.

Note: this module intentionally does not use ``from __future__ import
annotations`` — FastAPI resolves route signatures with ``get_type_hints``.
"""

import hmac
import os

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine

from signalweek.sources import (
    CATEGORY_HINTS,
    SOURCE_KINDS,
    SourceSpec,
    source_candidates_table,
    sources_table,
    upsert_sources,
)

ADMIN_TOKEN_ENV_VAR = "SIGNALWEEK_ADMIN_TOKEN"
ADMIN_TOKEN_HEADER = "X-Admin-Token"


def resolve_admin_token(explicit: str | None = None) -> str | None:
    """Return the configured admin token, or ``None`` when disabled.

    Precedence: explicit argument > ``SIGNALWEEK_ADMIN_TOKEN`` env var.
    Empty/whitespace values are treated as unset.
    """
    if explicit is not None:
        stripped = explicit.strip()
        return stripped or None
    raw = os.environ.get(ADMIN_TOKEN_ENV_VAR)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


class AddSourceRequest(BaseModel):
    """Payload accepted by ``POST /admin/sources``."""

    url: str = Field(..., min_length=1, max_length=2048)
    kind: str = Field(...)
    category_hint: str = Field(...)
    name: str | None = Field(default=None, max_length=255)


def build_admin_router(engine_provider, admin_token: str | None) -> APIRouter:
    """Build the ``/admin`` router.

    ``engine_provider`` is a zero-arg callable that returns the SQLAlchemy
    :class:`Engine` to use for each request — this matches the lazy engine
    resolution used elsewhere in :mod:`signalweek.web.app`. When
    ``admin_token`` is ``None`` every endpoint refuses with HTTP 503.
    """
    router = APIRouter(prefix="/admin", tags=["admin"])

    def require_admin_token(
        x_admin_token: str | None = Header(default=None, alias=ADMIN_TOKEN_HEADER),
    ) -> None:
        if admin_token is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="admin API is not configured",
            )
        if x_admin_token is None or not hmac.compare_digest(x_admin_token, admin_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing admin token",
            )

    auth = Depends(require_admin_token)

    @router.get("/sources", dependencies=[auth])
    def list_sources() -> dict:
        engine: Engine = engine_provider()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    sources_table.c.id,
                    sources_table.c.url,
                    sources_table.c.kind,
                    sources_table.c.category_hint,
                    sources_table.c.active,
                    sources_table.c.discovered,
                ).order_by(sources_table.c.id.asc())
            ).all()
        return {
            "sources": [
                {
                    "id": int(row.id),
                    "url": row.url,
                    "kind": row.kind,
                    "category_hint": row.category_hint,
                    "active": bool(row.active),
                    "discovered": bool(row.discovered),
                }
                for row in rows
            ]
        }

    @router.post("/sources", dependencies=[auth])
    def add_source(payload: AddSourceRequest):
        if payload.kind not in SOURCE_KINDS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"kind must be one of {sorted(SOURCE_KINDS)}",
            )
        if payload.category_hint not in CATEGORY_HINTS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"category_hint must be one of {sorted(CATEGORY_HINTS)}",
            )
        url = payload.url.strip()
        if not url:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="url must not be blank",
            )
        spec = SourceSpec(
            url=url,
            kind=payload.kind,
            category_hint=payload.category_hint,
            name=payload.name.strip() if payload.name else None,
        )
        engine: Engine = engine_provider()
        with engine.begin() as conn:
            result = upsert_sources(conn, [spec])
            row = conn.execute(
                select(
                    sources_table.c.id,
                    sources_table.c.url,
                    sources_table.c.kind,
                    sources_table.c.category_hint,
                    sources_table.c.active,
                    sources_table.c.discovered,
                ).where(sources_table.c.url == url)
            ).one()

        if result.inserted:
            outcome = "inserted"
            http_status = status.HTTP_201_CREATED
        elif result.updated:
            outcome = "updated"
            http_status = status.HTTP_200_OK
        else:
            outcome = "unchanged"
            http_status = status.HTTP_200_OK

        body = {
            "status": outcome,
            "source": {
                "id": int(row.id),
                "url": row.url,
                "kind": row.kind,
                "category_hint": row.category_hint,
                "active": bool(row.active),
                "discovered": bool(row.discovered),
            },
        }
        return JSONResponse(status_code=http_status, content=body)

    @router.post("/sources/{source_id}/disable", dependencies=[auth])
    def disable_source(source_id: int) -> dict:
        engine: Engine = engine_provider()
        with engine.begin() as conn:
            row = conn.execute(
                select(sources_table.c.id, sources_table.c.url, sources_table.c.active).where(
                    sources_table.c.id == int(source_id)
                )
            ).first()
            if row is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"no source with id={source_id}",
                )
            already_inactive = not bool(row.active)
            if not already_inactive:
                conn.execute(
                    sources_table.update()
                    .where(sources_table.c.id == int(row.id))
                    .values(active=False)
                )
        return {
            "status": "already_inactive" if already_inactive else "disabled",
            "source": {
                "id": int(row.id),
                "url": row.url,
                "active": False,
            },
        }

    @router.get("/candidates", dependencies=[auth])
    def list_candidates() -> dict:
        engine: Engine = engine_provider()
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    source_candidates_table.c.id,
                    source_candidates_table.c.domain,
                    source_candidates_table.c.cite_count,
                    source_candidates_table.c.distinct_weeks_count,
                    source_candidates_table.c.first_seen_week,
                    source_candidates_table.c.last_seen_week,
                    source_candidates_table.c.promoted,
                    source_candidates_table.c.promoted_source_id,
                ).order_by(
                    source_candidates_table.c.promoted.asc(),
                    source_candidates_table.c.cite_count.desc(),
                    source_candidates_table.c.domain.asc(),
                )
            ).all()
        return {
            "candidates": [
                {
                    "id": int(row.id),
                    "domain": row.domain,
                    "cite_count": int(row.cite_count),
                    "distinct_weeks_count": int(row.distinct_weeks_count),
                    "first_seen_week": row.first_seen_week.isoformat(),
                    "last_seen_week": row.last_seen_week.isoformat(),
                    "promoted": bool(row.promoted),
                    "promoted_source_id": (
                        int(row.promoted_source_id) if row.promoted_source_id is not None else None
                    ),
                }
                for row in rows
            ]
        }

    return router


def register_admin_router(
    app: FastAPI,
    engine_provider,
    *,
    admin_token: str | None,
) -> None:
    """Attach the admin router to ``app``.

    The router is always attached: when the token is unset the endpoints
    respond with 503 rather than 404 so an operator gets a clear signal that
    they forgot to set ``SIGNALWEEK_ADMIN_TOKEN``.
    """
    app.include_router(build_admin_router(engine_provider, admin_token))
