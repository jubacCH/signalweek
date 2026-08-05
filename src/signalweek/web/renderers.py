"""Render the fixed 5-section issue page to a self-contained HTML string.

The renderer is intentionally decoupled from the FastAPI request cycle: it
accepts an issue's metadata plus a flat list of items and returns HTML. That
lets the build pipeline, tests, and the eventual public archive share one
code path.

The template extends ``base.html.j2`` which uses Starlette's ``url_for`` for
static assets. When rendering outside a request (tests, CLI, one-shot
builds) we provide a minimal ``url_for`` global so the base chrome keeps
resolving links to the mounted ``/static`` prefix.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime
from importlib.resources import files
from typing import Any

from jinja2 import Environment, FileSystemLoader

from signalweek.ingest.classify import CATEGORIES, CATEGORY_LABELS

_TEMPLATES_DIR = str(files("signalweek.web") / "templates")


def _static_url_for(endpoint: str, /, **params: Any) -> str:
    if endpoint == "static":
        return f"/static/{params.get('path', '')}"
    raise ValueError(f"renderers.url_for: unknown endpoint {endpoint!r}")


def _make_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(_TEMPLATES_DIR),
        autoescape=True,
    )
    env.globals["url_for"] = _static_url_for
    return env


def render_issue(
    *,
    week_of: date,
    status: str,
    published_at: datetime | None,
    items: Iterable[Mapping[str, Any]],
) -> str:
    """Render one issue into the fixed 5-section HTML page.

    ``items`` is a flat iterable of mapping-like rows (dict or SQLAlchemy
    Row); each must expose ``category``, ``position``, ``headline``,
    ``summary``, ``primary_url``, and ``extra_source_urls``. Items are
    grouped by category and, within each category, sorted by ``position``
    ascending. Every category in :data:`CATEGORIES` renders its heading even
    when empty — the fixed taxonomy is part of the contract.
    """
    grouped: dict[str, list[Mapping[str, Any]]] = {cat: [] for cat in CATEGORIES}
    for item in items:
        category = item["category"]
        if category not in grouped:
            raise ValueError(f"unknown category {category!r}; expected one of {CATEGORIES}")
        grouped[category].append(item)
    for cat in CATEGORIES:
        grouped[cat].sort(key=lambda it: it["position"])

    env = _make_env()
    template = env.get_template("issue.html.j2")
    return template.render(
        title=f"Week of {week_of.isoformat()}",
        week_of=week_of,
        status=status,
        published_at=published_at,
        categories=CATEGORIES,
        category_labels=CATEGORY_LABELS,
        items_by_category=grouped,
        nav_current="archive",
    )
