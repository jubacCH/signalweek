"""Smoke tests verifying the project scaffold is importable and deps are present."""

import importlib

import signalweek


def test_package_version_exposed():
    assert signalweek.__version__ == "0.1.0"


def test_core_dependencies_importable():
    for module_name in (
        "fastapi",
        "sqlalchemy",
        "httpx",
        "feedparser",
        "apscheduler",
        "jinja2",
    ):
        importlib.import_module(module_name)
