"""Smoke tests proving the toolchain (Python 3.12 + pytest) runs."""

from __future__ import annotations

import sys


def test_python_version_is_312() -> None:
    assert sys.version_info[:2] == (3, 12)


def test_arithmetic_sanity() -> None:
    assert 2 + 2 == 4


def test_import_stdlib() -> None:
    import json

    assert json.loads('{"ok": true}') == {"ok": True}
