"""Wiring/integration tests.

The unit tests exercise ``run_ingest`` / ``build_issue`` / ``create_scheduler``
in isolation — which is exactly why the production gap (nobody *started* the
scheduler, nothing *seeded* the sources) slipped through. These tests assert the
app actually wires those into startup, so the deployed product is not a dead
pipeline.
"""
from __future__ import annotations

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi.testclient import TestClient
from sqlalchemy import select

from signalweek.sources import (
    DEFAULT_SOURCES_YAML,
    load_sources_yaml,
    seed_sources_if_empty,
    sources_table,
)
from signalweek.web.app import create_app


def test_packaged_sources_yaml_ships_and_loads():
    # the registry the built image relies on must be resolvable + non-trivial
    assert DEFAULT_SOURCES_YAML.exists()
    assert len(load_sources_yaml()) >= 20


def test_seed_sources_if_empty_is_idempotent(curated_engine):
    with curated_engine.begin() as conn:
        first = seed_sources_if_empty(conn)
    assert first >= 20
    with curated_engine.begin() as conn:
        second = seed_sources_if_empty(conn)
    assert second == 0
    with curated_engine.begin() as conn:
        total = len(conn.execute(select(sources_table.c.id)).all())
    assert total == first


def test_app_startup_seeds_and_starts_scheduler(tmp_path):
    # a file-backed engine so the lifespan (run in the TestClient's worker
    # thread) and the assertions share the same SQLite database
    from signalweek.db.session import create_db_engine
    from signalweek.sources import sources_metadata

    engine = create_db_engine(f"sqlite:///{tmp_path / 'wiring.db'}")
    sources_metadata.create_all(engine)
    try:
        sched = BackgroundScheduler()
        app = create_app(engine=engine, start_background=True, scheduler=sched)
        # entering the TestClient context runs the lifespan startup hook
        with TestClient(app):
            assert app.state.scheduler is sched
            assert sched.running
            job_ids = {j.id for j in sched.get_jobs()}
            assert "ingest" in job_ids
            assert "weekly_pipeline" in job_ids
            with engine.begin() as conn:
                seeded = len(conn.execute(select(sources_table.c.id)).all())
            assert seeded >= 20
        # leaving the context runs the shutdown hook
        assert not sched.running
    finally:
        engine.dispose()


def test_app_without_background_does_not_start_scheduler(curated_engine):
    # tests / callers that inject an engine must not spin up a scheduler
    app = create_app(engine=curated_engine)  # start_background defaults off when engine given
    with TestClient(app):
        assert getattr(app.state, "scheduler", None) is None
