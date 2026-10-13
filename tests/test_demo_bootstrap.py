"""The hosted demo seeds itself, which means it writes without being asked.

That is the one place in the system where that is true, so the invariants get
tested rather than assumed: it creates data when there is none, it leaves
existing data alone, and running it again changes nothing. The last one is what
keeps a Streamlit rerun — which re-executes the whole script on every widget
interaction — from reseeding the database under a visitor's feet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_control_plane.adapters.synthetic import SyntheticSource
from agent_control_plane.bootstrap import build_control_plane
from agent_control_plane.config import REPO_ROOT, Settings
from agent_control_plane.demo import DEMO_CREDIT_RATE_USD, DEMO_SEED, ensure_demo_data
from agent_control_plane.demo import seed as seed_demo_dataset
from agent_control_plane.models.enums import Platform


@pytest.fixture
def demo_settings(tmp_path: Path) -> Settings:
    """A throwaway database, but the real committed fixtures — the point of the
    bootstrap is that it builds the *shipped* demo, not a reduced one."""
    return Settings(
        db_path=tmp_path / "demo.db",
        data_dir=REPO_ROOT / "data",
        registry_dir=REPO_ROOT / "config",
    )


def test_it_creates_the_demo_when_the_database_is_absent(demo_settings: Settings) -> None:
    assert not demo_settings.db_path.exists()

    state = ensure_demo_data(demo_settings)

    assert state.seeded
    assert state.event_count > 0
    assert demo_settings.db_path.exists()


def test_it_builds_every_shipped_source(demo_settings: Settings) -> None:
    """A demo missing MCP or Snowflake would still render, and would quietly
    misrepresent what the project integrates with."""
    ensure_demo_data(demo_settings)

    with build_control_plane(demo_settings) as cp:
        platforms = cp.analytics.events_by_platform()

    assert {Platform.MCP.value, Platform.SNOWFLAKE.value} <= set(platforms)


def test_it_matches_what_the_cli_seed_produces(demo_settings: Settings, tmp_path: Path) -> None:
    """The console's self-seeding and `acp seed` must not drift. Same scenario
    seed, same fixtures, same credit rate — therefore the same event count."""
    ensure_demo_data(demo_settings)
    with build_control_plane(demo_settings) as cp:
        bootstrapped = cp.repository.count_events(cp.tenant_id)

    explicit = demo_settings.model_copy(update={"db_path": tmp_path / "explicit.db"})
    with build_control_plane(explicit) as cp:
        seed_demo_dataset(cp, seed_value=DEMO_SEED, credit_rate_usd=DEMO_CREDIT_RATE_USD)
        assert cp.repository.count_events(cp.tenant_id) == bootstrapped


def test_it_leaves_an_existing_database_alone(demo_settings: Settings) -> None:
    """Not merely 'does not crash': the second call must report that it found
    data and did nothing, because the alternative is a visitor's page being
    rebuilt mid-session."""
    first = ensure_demo_data(demo_settings)
    assert first.seeded

    second = ensure_demo_data(demo_settings)

    assert not second.seeded
    assert second.event_count == first.event_count


def test_repeated_initialisation_is_idempotent(demo_settings: Settings) -> None:
    """A Streamlit rerun fires this on every widget interaction."""
    counts = [ensure_demo_data(demo_settings).event_count for _ in range(4)]

    assert len(set(counts)) == 1, f"event count moved across reruns: {counts}"


def test_it_does_not_reseed_a_database_someone_else_populated(
    demo_settings: Settings,
) -> None:
    """Any events at all mean hands off. A partially ingested database belongs
    to whoever was ingesting into it, not to the demo."""
    with build_control_plane(demo_settings) as cp:
        cp.ingestion.ingest(SyntheticSource(seed=1, tenant_id=cp.tenant_id))
        planted = cp.repository.count_events(cp.tenant_id)

    state = ensure_demo_data(demo_settings)

    assert not state.seeded
    assert state.event_count == planted


def test_the_default_settings_turn_the_bootstrap_on() -> None:
    """This is what makes the hosted demo work with no configuration at all."""
    assert Settings().demo_bootstrap is True


def test_the_bootstrap_can_be_turned_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The separation a future real-telemetry deployment needs."""
    monkeypatch.setenv("ACP_DEMO_BOOTSTRAP", "false")

    assert Settings().demo_bootstrap is False
