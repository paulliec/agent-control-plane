"""Ingestion -> persistence -> policy evaluation -> analytics, wired the way the
CLI wires it, against the registry that is actually committed to config/."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_control_plane.adapters.mcp import MCPCallLogSource
from agent_control_plane.adapters.synthetic import SyntheticSource, generate_events
from agent_control_plane.bootstrap import ControlPlane, build_control_plane
from agent_control_plane.config import REPO_ROOT, Settings
from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.enums import AccessScopeMode, EventType, Severity
from agent_control_plane.registry import Registry, RegistryError, load_registry

SAMPLE_LOG = REPO_ROOT / "data" / "samples" / "mcp_tool_calls.jsonl"


@pytest.fixture
def seeded(control_plane: ControlPlane) -> ControlPlane:
    # Wired exactly as `acp seed` wires it — including the vocabulary, without
    # which the adapter passes vendor claims through and half the domain
    # governance never runs.
    control_plane.ingestion.ingest_all(
        [
            SyntheticSource(),
            MCPCallLogSource(
                SAMPLE_LOG,
                tenant_id=control_plane.tenant_id,
                domains=control_plane.registry.data_domains,
            ),
        ]
    )
    return control_plane


# -- the shipped registry -------------------------------------------------


def test_shipped_registry_loads(shipped_registry: Registry) -> None:
    """A typo in governance config should fail the build, not production."""
    assert "agt_forecast_weekly" in shipped_registry.agents
    assert "agt_pricing_experiment" not in shipped_registry.agents  # shadow, on purpose

    forecaster = shipped_registry.agents["agt_forecast_weekly"].governance
    assert forecaster.tool_access.mode is AccessScopeMode.ALLOWLIST
    assert "inventory.adjust_stock" not in forecaster.tool_access.allowed
    assert not forecaster.data_access.permits("hr")


def test_shipped_tool_registry_flags_the_privileged_tools(shipped_registry: Registry) -> None:
    for name in ("inventory.adjust_stock", "reporting.publish"):
        tool = shipped_registry.tools[name]
        assert tool.is_mutating and tool.requires_approval, name
    assert shipped_registry.tools["sales.query_history"].is_mutating is False


def test_every_tool_the_scenario_uses_is_registered(shipped_registry: Registry) -> None:
    """Otherwise the read-only rule loses its second detection signal."""
    used = {e.tool_name for e in generate_events() if e.tool_name}
    assert used <= set(shipped_registry.tools)


def test_duplicate_agent_ids_rejected(tmp_path: Path) -> None:
    agents = tmp_path / "agents.yaml"
    agents.write_text(
        "agents:\n"
        "  - {agent_id: a, name: A, platform: local}\n"
        "  - {agent_id: a, name: B, platform: local}\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="duplicate agent_id"):
        load_registry(agents, tmp_path / "missing_tools.yaml")


# -- ingestion ------------------------------------------------------------


def test_ingest_persists_and_evaluates(control_plane: ControlPlane) -> None:
    tenant = control_plane.tenant_id
    result = control_plane.ingestion.ingest(SyntheticSource())

    assert result.fetched == result.written > 0
    assert result.duplicates == 0
    assert control_plane.repository.count_events(tenant) == result.written
    assert result.violations > 0
    assert control_plane.repository.list_policy_results(
        tenant, correlation_id=result.correlation_ids[0]
    )


def test_ingest_is_idempotent(control_plane: ControlPlane) -> None:
    first = control_plane.ingestion.ingest(SyntheticSource())
    second = control_plane.ingestion.ingest(SyntheticSource())

    assert second.written == 0
    assert second.duplicates == second.fetched
    assert control_plane.repository.count_events(control_plane.tenant_id) == first.written


def test_reevaluation_replaces_results_rather_than_accumulating(
    control_plane: ControlPlane,
) -> None:
    tenant = control_plane.tenant_id
    control_plane.ingestion.ingest(SyntheticSource())
    before = len(control_plane.repository.list_policy_results(tenant))
    control_plane.ingestion.evaluate_everything()
    assert len(control_plane.repository.list_policy_results(tenant)) == before


def test_ingestion_stamps_provenance(control_plane: ControlPlane) -> None:
    control_plane.ingestion.ingest(SyntheticSource())
    events = control_plane.repository.list_events(control_plane.tenant_id, limit=5)
    assert all(e.ingested_at is not None for e in events)
    assert all(e.source_adapter == "synthetic" for e in events)


def test_two_sources_coexist(seeded: ControlPlane) -> None:
    adapters = {e.source_adapter for e in seeded.repository.list_events(seeded.tenant_id)}
    assert adapters == {"synthetic", "mcp"}


# -- tenancy --------------------------------------------------------------


def test_a_second_tenant_boots_and_sees_none_of_the_first(
    seeded: ControlPlane, settings: Settings
) -> None:
    """The shipped registries all belong to the default tenant, so a second
    tenant starts empty — it must not fail to build, and must not read across."""
    with build_control_plane(settings, tenant_id="acme") as acme:
        assert acme.registry.agents == {}
        assert acme.registry.data_domains.domains == {}
        assert acme.repository.count_events("acme") == 0
        assert acme.analytics.overview().total_events == 0

    assert seeded.repository.count_events(seeded.tenant_id) > 0


# -- policy outcomes on the real scenario ---------------------------------


def _run_matching(cp: ControlPlane, fragment: str) -> str:
    return next(c for c in cp.repository.list_correlation_ids(cp.tenant_id) if fragment in c)


def _violation_names(cp: ControlPlane, correlation_id: str) -> set[str]:
    return {
        r.policy_name
        for r in cp.repository.list_policy_results(
            cp.tenant_id, correlation_id=correlation_id, violations_only=True
        )
    }


def test_clean_runs_produce_no_violations(seeded: ControlPlane) -> None:
    for correlation_id in seeded.repository.list_correlation_ids(seeded.tenant_id):
        if "clean" in correlation_id or "degraded" in correlation_id:
            assert _violation_names(seeded, correlation_id) == set(), correlation_id


def test_governed_publish_passes_the_approval_rule(seeded: ControlPlane) -> None:
    """Proves the approval rule can pass, not just fire."""
    run = _run_matching(seeded, "governed_publish")
    assert _violation_names(seeded, run) == set()

    results = seeded.repository.list_policy_results(seeded.tenant_id, correlation_id=run)
    approval = next(r for r in results if r.policy_name == "missing_human_approval")
    assert approval.passed


def test_violation_run_trips_every_intended_rule(seeded: ControlPlane) -> None:
    run = _run_matching(seeded, "violation")
    assert _violation_names(seeded, run) == {
        "read_only_violation",
        "unauthorized_data_domain",
        "tool_not_allowlisted",
        "missing_human_approval",
        "cost_threshold_exceeded",
        "excessive_tool_calls",
    }


def test_retry_storm_is_one_finding_not_eight(seeded: ControlPlane) -> None:
    """Occurrence count belongs in the evidence, not in the violation count."""
    run = _run_matching(seeded, "violation")
    findings = [
        r
        for r in seeded.repository.list_policy_results(
            seeded.tenant_id, correlation_id=run, violations_only=True
        )
        if r.policy_name == "tool_not_allowlisted"
        and r.evidence["tool_name"] == "inventory.read_shipments"
    ]
    assert len(findings) == 1
    assert findings[0].evidence["occurrences"] == 8
    assert len(findings[0].evidence["event_ids"]) == 8


def test_denied_write_still_recorded_as_critical(seeded: ControlPlane) -> None:
    run = _run_matching(seeded, "violation")
    finding = next(
        r
        for r in seeded.repository.list_policy_results(
            seeded.tenant_id, correlation_id=run, violations_only=True
        )
        if r.policy_name == "read_only_violation"
    )
    assert finding.severity is Severity.CRITICAL
    assert finding.evidence["tool_name"] == "inventory.adjust_stock"
    assert finding.evidence["statuses"] == {"denied": 1}
    assert finding.evidence["requests"][0]["sku"] == "BEV-1042"


def test_shadow_agent_is_the_only_finding_in_its_run(seeded: ControlPlane) -> None:
    run = _run_matching(seeded, "shadow")
    assert _violation_names(seeded, run) == {"unregistered_agent"}


def test_mcp_run_catches_the_publish_attempt(seeded: ControlPlane) -> None:
    assert _violation_names(seeded, "mcp-analyst-2026-08-06") == {
        "read_only_violation",
        "tool_not_allowlisted",
        "unauthorized_data_domain",
        "missing_human_approval",
        "unknown_data_domain",
    }


def test_an_unclassifiable_resource_surfaces_as_our_gap_not_theirs(
    seeded: ControlPlane,
) -> None:
    """`erp://returns/rma_queue` has no mapping rule. The finding says so, and
    it is the one violation in the run we would not have blocked."""
    finding = next(
        r
        for r in seeded.repository.list_policy_results(
            seeded.tenant_id, correlation_id="mcp-analyst-2026-08-06", violations_only=True
        )
        if r.policy_name == "unknown_data_domain"
    )
    assert finding.evidence["reason"] == "no_mapping_rule"
    assert finding.evidence["resources"] == ["erp://returns/rma_queue"]
    assert finding.would_block is False


def test_every_rule_reports_on_every_run(seeded: ControlPlane) -> None:
    """'No violations' must never be confusable with 'nobody checked'."""
    expected = {rule.name for rule in seeded.engine.rules}
    for correlation_id in seeded.repository.list_correlation_ids(seeded.tenant_id):
        reported = {
            r.policy_name
            for r in seeded.repository.list_policy_results(
                seeded.tenant_id, correlation_id=correlation_id
            )
        }
        assert reported == expected, correlation_id


# -- analytics ------------------------------------------------------------


def test_overview_adds_up(seeded: ControlPlane) -> None:
    o = seeded.analytics.overview()

    assert o.total_events == seeded.repository.count_events(seeded.tenant_id)
    assert o.total_runs == len(seeded.repository.list_correlation_ids(seeded.tenant_id))
    assert o.successful_runs + o.failed_runs == o.total_runs
    assert o.total_cost_usd > 0
    assert o.unregistered_agents == 1
    assert o.critical_violations >= 1
    assert o.failed_tool_calls > 0


def test_a_recovered_retry_does_not_mark_the_run_failed(seeded: ControlPlane) -> None:
    """Run status follows the terminal event, not any failure inside it."""
    run = _run_matching(seeded, "degraded")
    summary = seeded.analytics.run_summary(run)
    assert summary is not None
    assert summary.ok is True
    events = seeded.repository.list_events(seeded.tenant_id, correlation_id=run)
    assert any(e.status.value == "timeout" for e in events)


def test_recent_runs_are_newest_first(seeded: ControlPlane) -> None:
    runs = seeded.analytics.recent_runs()
    assert [r.started_at for r in runs] == sorted((r.started_at for r in runs), reverse=True)
    worst = next(r for r in runs if "violation" in r.correlation_id)
    assert worst.max_severity is Severity.CRITICAL
    assert worst.ok is False


def test_top_tools_ranks_by_call_count(seeded: ControlPlane) -> None:
    tools = seeded.analytics.top_tools(5)
    assert [t.call_count for t in tools] == sorted((t.call_count for t in tools), reverse=True)
    shipments = next(
        t for t in seeded.analytics.top_tools(20) if t.tool_name == "inventory.read_shipments"
    )
    assert shipments.failure_count > 0


def test_cost_by_agent_ranks_the_forecaster_first(seeded: ControlPlane) -> None:
    costs = seeded.analytics.cost_by_agent()
    assert costs[0].agent_id == "agt_forecast_weekly"
    assert costs[0].violation_count > 0
    assert costs[0].run_count > 1


def test_platform_breakdown_covers_every_platform_seen(seeded: ControlPlane) -> None:
    by_platform = seeded.analytics.events_by_platform()
    assert set(by_platform) == {"local", "mcp", "snowflake", "other"}
    assert sum(by_platform.values()) == seeded.repository.count_events(seeded.tenant_id)


def test_cost_by_data_domain_ranks_by_spend(seeded: ControlPlane) -> None:
    spend = seeded.analytics.cost_by_data_domain()
    assert list(spend.values()) == sorted(spend.values(), reverse=True)
    assert set(spend) <= set(seeded.registry.data_domains.domains) | {UNKNOWN_DATA_DOMAIN}


def test_violations_sorted_worst_first(seeded: ControlPlane) -> None:
    violations = seeded.analytics.violations()
    assert violations[0].severity is Severity.CRITICAL
    ranks = [v.severity.rank for v in violations]
    assert ranks == sorted(ranks, reverse=True)


def test_chain_inspection_reaches_the_leaf(seeded: ControlPlane) -> None:
    run = _run_matching(seeded, "clean")
    chain = seeded.analytics.chain(run)
    assert chain is not None

    walked = chain.walk()
    assert len(walked) == len(chain.events)
    assert walked[0][1].event_type is EventType.AGENT_RUN_START

    depths = {e.resource: d for d, e in walked}
    # Human -> agent(0) -> tool(1) -> API(2)
    assert depths["https://api.weather.gov/gridpoints/forecast"] == 2
