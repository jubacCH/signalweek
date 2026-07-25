"""Tests for the ``signalweek`` admin CLI."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from signalweek.cli import main


class _RecordingRunner:
    """A stand-in for :func:`run_weekly_job` that records its kwargs."""

    def __init__(self, result: list[int]) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> list[int]:
        self.calls.append(kwargs)
        return self.result


def test_run_week_defaults_to_none_week_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = _RecordingRunner(result=[1, 2, 3])
    rc = main(["run-week"], runner=runner)

    assert rc == 0
    assert runner.calls == [{"week_start": None}]
    out = capsys.readouterr().out
    assert "3" in out
    assert "previous week" in out


def test_run_week_accepts_explicit_monday(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _RecordingRunner(result=[42])
    rc = main(["run-week", "--week-start", "2026-07-13"], runner=runner)

    assert rc == 0
    assert runner.calls == [{"week_start": date(2026, 7, 13)}]
    assert "2026-07-13" in capsys.readouterr().out


def test_run_week_rejects_non_monday(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _RecordingRunner(result=[])
    with pytest.raises(SystemExit) as exc:
        main(["run-week", "--week-start", "2026-07-15"], runner=runner)  # Wednesday

    assert exc.value.code == 2
    assert runner.calls == []
    assert "Monday" in capsys.readouterr().err


def test_run_week_rejects_bad_date_format(capsys: pytest.CaptureFixture[str]) -> None:
    runner = _RecordingRunner(result=[])
    with pytest.raises(SystemExit) as exc:
        main(["run-week", "--week-start", "not-a-date"], runner=runner)

    assert exc.value.code == 2
    assert runner.calls == []
    assert "invalid ISO date" in capsys.readouterr().err


def test_missing_subcommand_errors() -> None:
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
