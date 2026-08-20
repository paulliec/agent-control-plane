"""The CLI is the only interface at Tollgate 1, so its happy paths are tested
rather than only smoke-checked by hand."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_control_plane.cli import main
from agent_control_plane.config import REPO_ROOT

SAMPLE_LOG = REPO_ROOT / "data" / "samples" / "mcp_tool_calls.jsonl"


@pytest.fixture
def db(tmp_path: Path) -> str:
    return str(tmp_path / "cli.db")


def _run(db: str, *args: str) -> int:
    return main(["--db", db, "--log-level", "ERROR", *args])


@pytest.fixture
def seeded_db(db: str) -> str:
    assert _run(db, "seed", "--mcp-log", str(SAMPLE_LOG)) == 0
    return db


def test_seed_reports_every_source(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(db, "seed", "--mcp-log", str(SAMPLE_LOG)) == 0
    out = capsys.readouterr().out
    assert "synthetic" in out and "mcp" in out and "snowflake" in out
    assert "written=143" in out
    assert Path(db).exists()


def test_a_missing_optional_sample_degrades_to_a_warning(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """File-backed sources are optional; synthetic alone must still seed."""
    assert _run(db, "seed", "--snowflake-extract", "does/not/exist.jsonl") == 0
    out = capsys.readouterr().out
    assert "no Snowflake extract" in out
    assert "synthetic" in out


def test_snowflake_costs_are_unpriced_unless_a_rate_is_given(
    db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Credits are telemetry; the USD rate is contractual. Absent a rate the CLI
    must not quietly invent one, and must say so when it is handed one."""
    assert _run(db, "seed", "--mcp-log", str(SAMPLE_LOG)) == 0
    assert "assumption, not telemetry" not in capsys.readouterr().out

    assert _run(db, "seed", "--snowflake-credit-rate", "3.00") == 0
    assert "assumption, not telemetry" in capsys.readouterr().out


def test_seed_without_an_mcp_log_still_works(
    db: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing optional source should degrade, not crash."""
    assert _run(db, "seed", "--mcp-log", str(tmp_path / "nope.jsonl")) == 0
    out = capsys.readouterr().out
    assert "no MCP log" in out
    assert "synthetic" in out


def test_seed_is_idempotent(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(db, "seed", "--mcp-log", str(SAMPLE_LOG))
    capsys.readouterr()
    _run(db, "seed", "--mcp-log", str(SAMPLE_LOG))
    assert "written=0" in capsys.readouterr().out


def test_reset_starts_from_scratch(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(db, "seed", "--mcp-log", str(SAMPLE_LOG))
    capsys.readouterr()
    _run(db, "seed", "--reset", "--mcp-log", str(SAMPLE_LOG))
    out = capsys.readouterr().out
    assert "removed" in out
    assert "written=143" in out


def test_stats(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(seeded_db, "stats") == 0
    out = capsys.readouterr().out
    for expected in (
        "runs",
        "policy violations",
        "critical violations",
        "events by platform",
        "top tools",
        "cost by agent",
        "agt_forecast_weekly",
    ):
        assert expected in out


def test_runs_lists_every_run_newest_first(
    seeded_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(seeded_db, "runs") == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    # 9 synthetic + 1 MCP + 1 Snowflake.
    assert len(lines) == 11

    dates = [line.split()[0] for line in lines]
    assert dates == sorted(dates, reverse=True)

    worst = next(line for line in lines if "run-2026-08-17-violation" in line)
    assert "critical" in worst


def test_runs_respects_limit(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(seeded_db, "runs", "--limit", "3")
    assert len([x for x in capsys.readouterr().out.splitlines() if x.strip()]) == 3


def test_trace_renders_the_tree_and_its_violations(
    seeded_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(seeded_db, "trace", "run-2026-08-17-violation") == 0
    out = capsys.readouterr().out

    assert "agt_forecast_weekly" in out
    assert "agt_inventory_investigator" in out  # the delegated agent
    assert "inventory.adjust_stock" in out
    assert "denied" in out
    # worst first
    assert out.index("[critical]") < out.index("[high    ]")
    # grouping survives to the surface
    assert "8 times" in out


def test_trace_indentation_reflects_depth(
    seeded_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(seeded_db, "trace", "run-2026-08-03-clean")
    lines = capsys.readouterr().out.splitlines()
    api = next(x for x in lines if "api.weather.gov" in x)
    tool = next(x for x in lines if "weather.get_forecast" in x)
    assert len(api) - len(api.lstrip()) > len(tool) - len(tool.lstrip())


def test_trace_of_an_unknown_run_fails_cleanly(
    seeded_db: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(seeded_db, "trace", "no-such-run") == 1
    assert "no such run" in capsys.readouterr().err


def test_violations_worst_first(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(seeded_db, "violations") == 0
    out = capsys.readouterr().out
    assert out.startswith("[critical]")
    assert "read_only_violation" in out


def test_evaluate_is_idempotent(seeded_db: str, capsys: pytest.CaptureFixture[str]) -> None:
    _run(seeded_db, "evaluate")
    first = capsys.readouterr().out
    _run(seeded_db, "evaluate")
    assert capsys.readouterr().out == first


def test_unknown_command_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["nonsense"])
    assert exc.value.code != 0
