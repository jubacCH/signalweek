"""Jinja2 renderers for HTML and Markdown digest output.

The HTML environment autoescapes; the Markdown one does not. Both share the
same in-package template loader so the templates ship with the wheel.
"""

from __future__ import annotations

from datetime import datetime
from functools import cache

from jinja2 import Environment, PackageLoader

from signalweek.digest.models import Digest

_HTML_TEMPLATE = "digest.html.j2"
_MARKDOWN_TEMPLATE = "digest.md.j2"


def _fmt_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.date().isoformat()


def _fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _configure(env: Environment) -> Environment:
    env.filters["fmt_date"] = _fmt_date
    env.filters["fmt_datetime"] = _fmt_datetime
    return env


@cache
def _html_env() -> Environment:
    return _configure(
        Environment(
            loader=PackageLoader("signalweek.digest", "templates"),
            autoescape=True,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    )


@cache
def _markdown_env() -> Environment:
    # trim_blocks/lstrip_blocks are intentionally off: the Markdown template
    # controls whitespace explicitly with ``-%}`` so summary lines land under
    # their bullet and sections stay visually separated by a blank line.
    return _configure(
        Environment(
            loader=PackageLoader("signalweek.digest", "templates"),
            autoescape=False,
            keep_trailing_newline=True,
        )
    )


def render_html(digest: Digest) -> str:
    """Render ``digest`` as a self-contained HTML document."""
    return _html_env().get_template(_HTML_TEMPLATE).render(digest=digest)


def render_markdown(digest: Digest) -> str:
    """Render ``digest`` as a Markdown document."""
    return _markdown_env().get_template(_MARKDOWN_TEMPLATE).render(digest=digest)
