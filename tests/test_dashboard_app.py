"""Smoke test for the Streamlit console.

Deliberately thin: this asserts that every view *executes* against a real seeded
database, not that any particular widget appeared. Row shaping is covered in
`test_dashboard_presenters.py` and query behaviour in
`test_dashboard_service.py`.

It earns its place because a served page and a rendering page are different
things. Streamlit serves a static shell over HTTP and runs the script over a
websocket on a ScriptRunner thread, so an app can answer 200 and health-check OK
while raising on every interaction. This caught exactly that: a cached, thread-
bound SQLite connection reused across rerun threads.

Skipped when the `dashboard` extra is not installed, so the core dev install
stays lean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit", reason="install the 'dashboard' extra to run these")

from streamlit.testing.v1 import AppTest

from agent_control_plane.adapters.mcp import MCPCallLogSource
from agent_control_plane.adapters.snowflake import SnowflakeActivitySource
from agent_control_plane.adapters.synthetic import SyntheticSource
from agent_control_plane.bootstrap import build_control_plane
from agent_control_plane.config import REPO_ROOT

APP = REPO_ROOT / "src" / "agent_control_plane" / "dashboard" / "app.py"
PAGES = ("Overview", "Runs", "Findings", "Trace", "Data domains & cost")

SAMPLE_LOG = REPO_ROOT / "data" / "samples" / "mcp_tool_calls.jsonl"
SNOWFLAKE_EXTRACT = REPO_ROOT / "data" / "samples" / "snowflake_activity.jsonl"


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real database with all three sources, pointed at by the env the app reads."""
    db_path = tmp_path / "console.db"
    monkeypatch.setenv("ACP_DB_PATH", str(db_path))
    monkeypatch.setenv("ACP_DATA_DIR", str(tmp_path))

    with build_control_plane(db_path=db_path) as cp:
        cp.ingestion.ingest_all(
            [
                SyntheticSource(),
                MCPCallLogSource(
                    SAMPLE_LOG, tenant_id=cp.tenant_id, domains=cp.registry.data_domains
                ),
                SnowflakeActivitySource(
                    SNOWFLAKE_EXTRACT,
                    tenant_id=cp.tenant_id,
                    domains=cp.registry.data_domains,
                    credit_rate_usd=3.00,
                ),
            ]
        )
    return db_path


def _run(page: str | None = None) -> AppTest:
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.run()
    assert not app.exception, f"initial render raised: {[e.value for e in app.exception]}"
    if page is not None:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, f"{page} raised: {[e.value for e in app.exception]}"
    return app


@pytest.mark.parametrize("page", PAGES)
def test_every_view_renders(seeded_db: Path, page: str) -> None:
    app = _run(page)
    assert app.metric, f"{page} rendered no metrics"


def test_navigating_between_views_reuses_no_stale_connection(seeded_db: Path) -> None:
    """The regression this file exists for. Streamlit reruns on a pool thread,
    so a connection cached across reruns lands on the wrong thread and raises.
    Walking every page in one session is what surfaces it."""
    app = _run()
    for page in PAGES:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, f"{page} raised on rerun: {[e.value for e in app.exception]}"


def test_the_overview_shows_all_three_sources(seeded_db: Path) -> None:
    app = _run("Overview")
    captions = " ".join(str(c.value) for c in app.caption)
    assert "Events by source" in captions
    assert "one control plane, not one per vendor" in captions


def test_findings_explain_attribution(seeded_db: Path) -> None:
    """The unknown-vs-unauthorized distinction has to be visible, not implied."""
    app = _run("Findings")
    notes = " ".join(str(i.value) for i in app.info)
    assert "Attribution matters" in notes
    assert "unknown_data_domain" in notes and "never blocks" in notes


def test_cost_is_labelled_as_estimated_and_partly_derived(seeded_db: Path) -> None:
    app = _run("Data domains & cost")
    warnings = " ".join(str(w.value) for w in app.warning)
    assert "derived" in warnings
    assert "not a figure the source reported" in warnings


def test_an_empty_database_explains_how_to_seed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reviewer who launches before seeding gets instructions, not a stack trace."""
    monkeypatch.setenv("ACP_DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setenv("ACP_DATA_DIR", str(tmp_path))

    app = AppTest.from_file(str(APP), default_timeout=120)
    app.run()
    assert not app.exception
    assert any("seed_demo" in str(i.value) for i in app.info)
