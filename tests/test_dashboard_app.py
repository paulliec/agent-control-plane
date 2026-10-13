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
from streamlit.testing.v1.element_tree import Button

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
    """With demo bootstrap off, an empty console must explain itself.

    Bootstrap is off for any deployment reading real telemetry, where no events
    means ingestion is broken. The console's job there is instructions, not a
    stack trace — and certainly not synthetic rows invented to look healthy.
    """
    monkeypatch.setenv("ACP_DB_PATH", str(tmp_path / "empty.db"))
    monkeypatch.setenv("ACP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ACP_DEMO_BOOTSTRAP", "false")

    app = AppTest.from_file(str(APP), default_timeout=120)
    app.run()
    assert not app.exception
    assert any("seed_demo" in str(i.value) for i in app.info)
    # Bootstrap off means nothing was written, not even an empty schema.
    assert not (tmp_path / "empty.db").exists()


# -- public demo ----------------------------------------------------------


def test_the_console_seeds_itself_and_renders_from_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a visitor to the hosted demo gets: an empty filesystem in, a
    populated Overview out, with no seed step and no credentials.

    Asserting on rendered metrics rather than on the database, because the
    failure that matters is a blank page — the bootstrap succeeding while the
    page still shows the 'go and seed it' notice would pass a DB-only check.
    """
    monkeypatch.setenv("ACP_DB_PATH", str(tmp_path / "fresh.db"))
    monkeypatch.setenv("ACP_DATA_DIR", str(REPO_ROOT / "data"))

    app = _run("Overview")

    assert app.metric, "auto-seeded console rendered no metrics"
    assert not any("seed_demo" in str(i.value) for i in app.info)
    assert (tmp_path / "fresh.db").exists()


def test_navigating_an_auto_seeded_console_does_not_reseed_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Streamlit re-executes the whole script on every interaction, so the
    bootstrap runs again on each one. Totals must not move."""
    monkeypatch.setenv("ACP_DB_PATH", str(tmp_path / "fresh.db"))
    monkeypatch.setenv("ACP_DATA_DIR", str(REPO_ROOT / "data"))

    app = _run("Overview")
    before = app.metric[0].value

    for page in PAGES:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception, f"{page} raised: {[e.value for e in app.exception]}"
    app.sidebar.radio[0].set_value("Overview").run()

    assert app.metric[0].value == before, "event total changed across reruns"


def test_the_demo_labels_itself_as_synthetic(seeded_db: Path) -> None:
    """A public visitor must not mistake modelled fixtures for a live feed."""
    app = _run("Overview")
    sidebar = " ".join(str(c.value) for c in app.sidebar.caption)

    assert "Demo environment" in sidebar
    assert "synthetic" in sidebar.lower()
    assert "Nothing here is connected to a live enterprise environment" in sidebar
    assert "evaluated enforcement intent" in sidebar


def test_a_real_telemetry_deployment_shows_no_demo_label(
    seeded_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The label describes where the data came from, so it has to disappear
    when the data no longer comes from fixtures."""
    monkeypatch.setenv("ACP_DEMO_BOOTSTRAP", "false")

    app = _run("Overview")
    sidebar = " ".join(str(c.value) for c in app.sidebar.caption)

    assert "Demo environment" not in sidebar


# -- needs attention ------------------------------------------------------


def _attention_buttons(app: AppTest) -> dict[str, Button]:
    return {str(b.key): b for b in app.button if b.key and b.key.startswith("attention_")}


def test_the_overview_leads_with_what_needs_attention(seeded_db: Path) -> None:
    app = _run("Overview")
    labels = [m.label for m in app.metric]
    assert "Critical findings" in labels
    assert "Unregistered agents" in labels
    assert set(_attention_buttons(app)) == {
        "attention_critical",
        "attention_would_block",
        "attention_unregistered_agent",
        "attention_unknown_data_domain",
    }


@pytest.mark.parametrize(
    ("button", "state_key", "expected"),
    [
        ("attention_critical", "f_sev", ["critical"]),
        ("attention_would_block", "f_block", True),
        ("attention_unregistered_agent", "f_policies", ["unregistered_agent"]),
        ("attention_unknown_data_domain", "f_policies", ["unknown_data_domain"]),
    ],
)
def test_an_attention_item_drills_into_its_findings(
    seeded_db: Path, button: str, state_key: str, expected: object
) -> None:
    """The click must land on Findings, pre-filtered, with rows on screen.

    Streamlit refuses to let a widget's key be written after that widget is
    built, so the drill-down queues its target and `main` applies it on the
    next run. A regression here fails silently — the button would simply do
    nothing — which is why this asserts on the landing state, not the click.
    """
    app = _run("Overview")
    _attention_buttons(app)[button].click().run()

    assert not app.exception, f"{button} raised: {[e.value for e in app.exception]}"
    assert app.session_state["page"] == "Findings"
    assert app.session_state[state_key] == expected
    assert len(app.dataframe[0].value) > 0, f"{button} landed on an empty table"


def test_a_second_drilldown_replaces_the_first(seeded_db: Path) -> None:
    """Filters must not accumulate across clicks, or the second drill-down
    silently shows the intersection of both."""
    app = _run("Overview")
    _attention_buttons(app)["attention_would_block"].click().run()
    app.sidebar.radio[0].set_value("Overview").run()
    _attention_buttons(app)["attention_critical"].click().run()

    assert not app.exception
    assert app.session_state["f_sev"] == ["critical"]
    assert app.session_state["f_block"] is False


def test_the_source_chart_separates_generated_from_integration(seeded_db: Path) -> None:
    """The demo scenario must not set the scale the real integrations are read on."""
    app = _run("Overview")
    captions = " ".join(str(c.value) for c in app.caption)
    assert "Generated demo scenario, on its own scale" in captions
    assert "mcp (10)" in captions and "snowflake (13)" in captions
    assert "nothing is excluded" in captions
