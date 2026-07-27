"""Sanity checks on the deploy artifacts (Dockerfile, compose, .env, README).

These tests deliberately stop at inspecting the shipped files. Actually
building the image or launching containers requires a Docker daemon which is
not available in the unit-test environment; the CI harness that does have one
picks the same files up.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def compose_text() -> str:
    return (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def env_example_text() -> str:
    return (REPO_ROOT / ".env.example").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def readme_text() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def entrypoint_path() -> Path:
    return REPO_ROOT / "docker" / "entrypoint.sh"


# ---------------------------------------------------------------------------
# Dockerfile
# ---------------------------------------------------------------------------


class TestDockerfile:
    def test_declares_at_least_two_build_stages(self, dockerfile_text: str) -> None:
        stages = re.findall(r"^FROM\s+\S+\s+AS\s+(\S+)", dockerfile_text, flags=re.MULTILINE)
        assert len(stages) >= 2, f"expected multi-stage build, saw stages={stages!r}"
        assert "builder" in stages
        assert "runtime" in stages

    def test_runtime_copies_venv_from_builder(self, dockerfile_text: str) -> None:
        # Multi-stage benefit: runtime image doesn't ship pip/setuptools/wheel
        # sources — only the compiled virtualenv from the builder stage.
        assert re.search(r"COPY\s+--from=builder\b[^\n]*/opt/venv\s+/opt/venv", dockerfile_text)

    def test_runs_as_non_root_user(self, dockerfile_text: str) -> None:
        assert re.search(r"^USER\s+signalweek\b", dockerfile_text, flags=re.MULTILINE)
        assert re.search(r"useradd[^\n]*signalweek", dockerfile_text)

    def test_declares_data_volume(self, dockerfile_text: str) -> None:
        assert re.search(r'^VOLUME\s+\[?"?/app/data"?\]?', dockerfile_text, flags=re.MULTILINE)

    def test_exposes_http_port(self, dockerfile_text: str) -> None:
        assert re.search(r"^EXPOSE\s+8000\b", dockerfile_text, flags=re.MULTILINE)

    def test_has_healthcheck_hitting_health_endpoint(self, dockerfile_text: str) -> None:
        assert "HEALTHCHECK" in dockerfile_text
        assert "/health" in dockerfile_text

    def test_entrypoint_wraps_default_uvicorn_command(self, dockerfile_text: str) -> None:
        assert re.search(r'ENTRYPOINT\s+\[\s*"signalweek-entrypoint"', dockerfile_text)
        assert re.search(r'CMD\s+\[[^\]]*"uvicorn"[^\]]*"signalweek\.main:app"', dockerfile_text)

    def test_default_database_url_points_at_persistent_volume(self, dockerfile_text: str) -> None:
        # If someone runs the image without compose the DB should still land on
        # /app/data (the declared VOLUME) rather than a throw-away working dir.
        assert 'DATABASE_URL="sqlite:////app/data/signalweek.db"' in dockerfile_text


# ---------------------------------------------------------------------------
# Entrypoint script
# ---------------------------------------------------------------------------


class TestEntrypointScript:
    def test_script_exists_and_is_executable(self, entrypoint_path: Path) -> None:
        assert entrypoint_path.is_file()
        assert os.access(entrypoint_path, os.X_OK), "entrypoint.sh must be executable"

    def test_runs_migrations_then_execs_command(self, entrypoint_path: Path) -> None:
        text = entrypoint_path.read_text(encoding="utf-8")
        assert text.startswith("#!/"), "must have a shebang"
        # Applies schema before starting whatever CMD is passed. Look at the
        # command lines (comments filtered out) so we compare positions of the
        # actual `alembic ...` and `exec "$@"` invocations.
        commands = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        alembic_line = next(
            (i for i, line in enumerate(commands) if line.startswith("alembic ")),
            None,
        )
        exec_line = next(
            (i for i, line in enumerate(commands) if line.startswith("exec ")),
            None,
        )
        assert alembic_line is not None, "entrypoint must run alembic"
        assert exec_line is not None, "entrypoint must exec the passed CMD"
        assert alembic_line < exec_line, "migrations must run before exec"
        assert "upgrade head" in text
        assert "set -e" in text, "entrypoint should fail fast if migrations fail"


# ---------------------------------------------------------------------------
# docker-compose.yml
# ---------------------------------------------------------------------------


class TestDockerCompose:
    def test_defines_app_service_that_builds_locally(self, compose_text: str) -> None:
        assert re.search(r"^\s*app:\s*$", compose_text, flags=re.MULTILINE)
        assert re.search(r"context:\s*\.", compose_text)

    def test_publishes_http_port(self, compose_text: str) -> None:
        assert re.search(r'"\$\{SIGNALWEEK_HTTP_PORT:-8000\}:8000"', compose_text)

    def test_uses_env_file_for_config(self, compose_text: str) -> None:
        # env_file: .env keeps deploy-time configuration out of the compose
        # file itself, which stays in git.
        assert re.search(r"env_file:\s*\n\s*-\s*\.env", compose_text)

    def test_persists_sqlite_via_named_volume(self, compose_text: str) -> None:
        # Named volume, not a bind mount — survives `docker compose down`
        # and doesn't collide with a stray ./data directory in the checkout.
        assert re.search(r"signalweek_data:\s*/app/data", compose_text)
        assert re.search(r"^volumes:\s*\n\s+signalweek_data:", compose_text, flags=re.MULTILINE)

    def test_restarts_unless_stopped(self, compose_text: str) -> None:
        assert re.search(r"restart:\s*unless-stopped", compose_text)

    def test_has_healthcheck(self, compose_text: str) -> None:
        assert "healthcheck:" in compose_text
        assert "/health" in compose_text

    def test_default_database_url_points_at_mounted_volume(self, compose_text: str) -> None:
        assert "sqlite:////app/data/signalweek.db" in compose_text


# ---------------------------------------------------------------------------
# .env.example
# ---------------------------------------------------------------------------


class TestEnvExample:
    REQUIRED_KEYS = {
        "DATABASE_URL",
        "SIGNALWEEK_HTTP_PORT",
    }

    def _keys(self, text: str) -> set[str]:
        found: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, _ = stripped.partition("=")
            found.add(key.strip())
        return found

    def test_documents_every_env_var_the_app_reads(self, env_example_text: str) -> None:
        assert self.REQUIRED_KEYS.issubset(self._keys(env_example_text))

    def test_does_not_document_retired_session_secret(self, env_example_text: str) -> None:
        # Session cookies were removed alongside the per-user surfaces; the
        # env template must no longer prompt operators for a session secret.
        assert "SESSION_SECRET" not in env_example_text

    def test_does_not_reference_anthropic_or_similar_api_key(self, env_example_text: str) -> None:
        # Guardrail: this repo should never persist an ANTHROPIC_API_KEY, nor
        # prompt operators to supply one, since the MVP doesn't call any LLM.
        lowered = env_example_text.lower()
        assert "anthropic_api_key" not in lowered
        assert "openai_api_key" not in lowered


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


class TestReadmeQuickstart:
    def test_has_quickstart_heading(self, readme_text: str) -> None:
        assert re.search(r"^##\s+Quickstart", readme_text, flags=re.MULTILINE)

    def test_documents_env_bootstrap_and_compose_up(self, readme_text: str) -> None:
        assert "cp .env.example .env" in readme_text
        assert "docker compose up" in readme_text

    def test_has_self_host_notes_section(self, readme_text: str) -> None:
        assert re.search(r"^##\s+Self-hosting notes", readme_text, flags=re.MULTILINE)

    def test_mentions_backup_and_https(self, readme_text: str) -> None:
        lowered = readme_text.lower()
        assert "backup" in lowered or "back it up" in lowered
        assert "https" in lowered

    def test_frames_product_as_curated_no_accounts(self, readme_text: str) -> None:
        # The pivot removed per-user sign-up — the quickstart must not tell
        # operators to point users at a sign-up flow that no longer exists.
        lowered = readme_text.lower()
        assert "sign up" not in lowered
        assert "signalweek run-week" not in readme_text


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------


def test_gitignore_excludes_env_file() -> None:
    # `.env` holds SESSION_SECRET in production — must not be committed.
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in {line.strip() for line in ignored}
