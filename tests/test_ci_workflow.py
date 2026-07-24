"""Validate the GitHub Actions CI workflow definition."""

from __future__ import annotations

from pathlib import Path

CI_WORKFLOW = (
    Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
)


def _read_workflow() -> str:
    assert CI_WORKFLOW.is_file(), f"CI workflow missing at {CI_WORKFLOW}"
    return CI_WORKFLOW.read_text()


def test_ci_workflow_file_exists() -> None:
    assert CI_WORKFLOW.is_file()


def test_ci_triggers_on_push_to_main() -> None:
    text = _read_workflow()
    assert "push:" in text
    push_block = text.split("push:", 1)[1].split("pull_request:", 1)[0]
    assert "main" in push_block


def test_ci_triggers_on_pull_request_to_main() -> None:
    text = _read_workflow()
    assert "pull_request:" in text
    pr_block = text.split("pull_request:", 1)[1].split("jobs:", 1)[0]
    assert "main" in pr_block


def test_ci_runs_ruff_check() -> None:
    assert "ruff check" in _read_workflow()


def test_ci_runs_mypy_on_src() -> None:
    assert "mypy src" in _read_workflow()


def test_ci_runs_pytest_quiet() -> None:
    assert "pytest -q" in _read_workflow()


def test_ci_uses_python_312() -> None:
    assert '"3.12"' in _read_workflow()
