"""The synthetic scenario is fixture *and* documentation: if it stops telling the
intended story, the demo and half the tests quietly become meaningless."""

from __future__ import annotations

from collections import Counter

from agentops.adapters.base import EventSource
from agentops.adapters.synthetic import (
    DEFAULT_SCHEDULE,
    SyntheticSource,
    generate_events,
)
from agentops.models.enums import ActionType, ActorType, EventType, Platform, Status
from agentops.models.event import build_chain


def test_source_satisfies_the_protocol() -> None:
    assert isinstance(SyntheticSource(), EventSource)


def test_generation_is_deterministic() -> None:
    first = generate_events(seed=7)
    second = generate_events(seed=7)
    assert [e.model_dump() for e in first] == [e.model_dump() for e in second]


def test_different_seeds_change_costs_but_not_structure() -> None:
    a, b = generate_events(seed=1), generate_events(seed=2)
    assert [e.event_id for e in a] == [e.event_id for e in b]  # ids are content-free
    assert [e.estimated_cost_usd for e in a] != [e.estimated_cost_usd for e in b]


def test_event_ids_are_unique() -> None:
    events = generate_events()
    assert len({e.event_id for e in events}) == len(events)


def test_one_run_per_scheduled_week() -> None:
    events = generate_events()
    runs = {e.correlation_id for e in events}
    assert len(runs) == len(DEFAULT_SCHEDULE)


def test_every_parent_reference_resolves() -> None:
    """A dangling parent silently truncates the trace view."""
    events = generate_events()
    ids = {e.event_id for e in events}
    dangling = [e for e in events if e.parent_event_id and e.parent_event_id not in ids]
    assert dangling == []


def test_each_run_has_exactly_one_root() -> None:
    events = generate_events()
    by_run: dict[str, list] = {}
    for event in events:
        by_run.setdefault(event.correlation_id, []).append(event)
    for correlation_id, run_events in by_run.items():
        chain = build_chain(correlation_id, run_events)
        assert len(chain.roots) == 1, correlation_id
        assert chain.roots[0].event_type is EventType.AGENT_RUN_START


def test_all_events_land_inside_their_own_run() -> None:
    for event in generate_events():
        assert event.correlation_id.startswith(("run-", "mcp-"))
        assert event.source_adapter == "synthetic"
        assert event.timestamp.tzinfo is not None


def test_human_to_agent_to_tool_to_api_chain_exists() -> None:
    """The canonical narrative: Human -> Forecast Agent -> Weather MCP -> API."""
    events = generate_events(schedule=("clean",))
    chain = build_chain(events[0].correlation_id, events)
    by_id = {e.event_id: e for e in events}

    api = next(e for e in events if e.resource == "https://api.weather.gov/gridpoints/forecast")
    tool = by_id[api.parent_event_id or ""]
    agent_start = by_id[tool.parent_event_id or ""]

    assert api.event_type is EventType.DATA_ACCESS
    assert api.platform is Platform.OTHER
    assert tool.tool_name == "weather.get_forecast"
    assert tool.platform is Platform.MCP
    assert tool.tool_server == "weather-mcp"
    assert agent_start.event_type is EventType.AGENT_RUN_START
    assert agent_start.actor_type is ActorType.HUMAN
    assert agent_start.actor_id == "paul.carson@contoso.com"
    assert agent_start.parent_event_id is None
    assert len(chain.walk()) == len(events)


def test_agent_to_agent_handoff_chain_exists() -> None:
    """Agent A -> Agent B -> MCP tool -> ERP."""
    events = generate_events(schedule=("clean",))
    by_id = {e.event_id: e for e in events}

    handoff = next(e for e in events if e.event_type is EventType.AGENT_HANDOFF)
    assert handoff.agent_id == "agt_forecast_weekly"
    assert handoff.action is ActionType.DELEGATE

    child_start = next(e for e in events if e.parent_event_id == handoff.event_id)
    assert child_start.agent_id == "agt_inventory_investigator"
    assert child_start.actor_id == "agt_forecast_weekly"
    assert child_start.actor_type is ActorType.AGENT

    erp = next(e for e in events if e.resource == "erp://inventory/stock_levels")
    stock_tool = by_id[erp.parent_event_id or ""]
    assert stock_tool.tool_name == "inventory.read_stock_levels"
    assert by_id[stock_tool.parent_event_id or ""].event_id == child_start.event_id


def test_clean_run_covers_the_whole_scenario() -> None:
    """Start, sales, promotions, weather, forecast, anomaly, investigation, report."""
    events = generate_events(schedule=("clean",))
    tools = {e.tool_name for e in events if e.tool_name}
    assert tools == {
        "sales.query_history",
        "promotions.get_calendar",
        "weather.get_forecast",
        "forecast.run_model",
        "inventory.read_stock_levels",
        "reporting.render_draft",
    }
    types = Counter(e.event_type for e in events)
    assert types[EventType.AGENT_RUN_START] == 2  # parent agent + delegated agent
    assert types[EventType.LLM_CALL] == 1
    assert types[EventType.AGENT_HANDOFF] == 1
    assert any(e.metadata.get("finding") == "unusual_sales_drop" for e in events)
    assert all(e.status is Status.SUCCESS for e in events)


def test_degraded_run_times_out_then_recovers() -> None:
    events = generate_events(schedule=("degraded",))
    weather = [e for e in events if e.tool_name == "weather.get_forecast"]
    assert [e.status for e in weather] == [Status.TIMEOUT, Status.SUCCESS]
    assert weather[0].error_code == "MCP_TIMEOUT"
    assert weather[1].metadata["attempt"] == 2
    # The run as a whole still succeeds — a recovered retry is not a failed run.
    end = next(e for e in events if e.event_type is EventType.AGENT_RUN_END)
    assert end.status is Status.SUCCESS


def test_violation_run_contains_all_four_intended_transgressions() -> None:
    events = generate_events(schedule=("violation",))

    # 1. unauthorised domain
    assert any(e.data_domain == "hr" for e in events)
    # 2. retry storm
    shipments = [e for e in events if e.tool_name == "inventory.read_shipments"]
    assert len(shipments) == 8
    assert sum(1 for e in shipments if e.status is Status.FAILURE) == 7
    # 3. read-only agent attempting a mutating, approval-gated tool
    adjust = next(e for e in events if e.tool_name == "inventory.adjust_stock")
    assert adjust.action is ActionType.WRITE
    assert adjust.status is Status.DENIED  # gateway blocked it; still a finding
    assert adjust.agent_id == "agt_forecast_weekly"
    # 4. cost blowout
    assert max(e.estimated_cost_usd or 0 for e in events) > 2.0
    # ...and the run ends badly
    end = next(e for e in events if e.event_type is EventType.AGENT_RUN_END
               and e.agent_id == "agt_forecast_weekly")
    assert end.status is Status.FAILURE


def test_governed_publish_run_links_its_approval() -> None:
    events = generate_events(schedule=("governed_publish",))
    approval = next(e for e in events if e.event_type is EventType.APPROVAL)
    publish = next(e for e in events if e.tool_name == "reporting.publish")

    assert approval.action is ActionType.APPROVE
    assert approval.actor_type is ActorType.HUMAN
    assert approval.metadata["approves_tool"] == "reporting.publish"
    assert publish.action is ActionType.WRITE
    assert publish.agent_id == "agt_report_publisher"


def test_shadow_run_uses_an_unregistered_agent() -> None:
    events = generate_events(schedule=("shadow",))
    assert {e.agent_id for e in events} == {"agt_pricing_experiment"}
    assert events[0].actor_type is ActorType.SERVICE


def test_since_filter_is_honoured() -> None:
    all_events = generate_events()
    cutoff = all_events[len(all_events) // 2].timestamp
    filtered = list(SyntheticSource().fetch(since=cutoff))
    assert filtered
    assert len(filtered) < len(all_events)
    assert all(e.timestamp >= cutoff for e in filtered)
