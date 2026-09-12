"""The console's query layer.

Everything the dashboard does that could be wrong lives here rather than in a
Streamlit callback, so it can be tested without rendering anything.
"""

from __future__ import annotations

import pytest

from agent_control_plane.adapters.mcp import MCPCallLogSource
from agent_control_plane.adapters.snowflake import SnowflakeActivitySource
from agent_control_plane.adapters.synthetic import SyntheticSource
from agent_control_plane.bootstrap import ControlPlane, build_control_plane, build_dashboard
from agent_control_plane.config import REPO_ROOT, Settings
from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.enums import Severity
from agent_control_plane.services.dashboard import (
    DashboardService,
    FindingFilter,
    RunFilter,
)

SAMPLE_LOG = REPO_ROOT / "data" / "samples" / "mcp_tool_calls.jsonl"
SNOWFLAKE_EXTRACT = REPO_ROOT / "data" / "samples" / "snowflake_activity.jsonl"
SNOWFLAKE_RUN = "sf-forecast-2026-08-18"
DEMO_CREDIT_RATE = 3.00


@pytest.fixture
def console(control_plane: ControlPlane) -> DashboardService:
    """All three sources, wired as `acp seed` wires them."""
    control_plane.ingestion.ingest_all(
        [
            SyntheticSource(),
            MCPCallLogSource(
                SAMPLE_LOG,
                tenant_id=control_plane.tenant_id,
                domains=control_plane.registry.data_domains,
            ),
            SnowflakeActivitySource(
                SNOWFLAKE_EXTRACT,
                tenant_id=control_plane.tenant_id,
                domains=control_plane.registry.data_domains,
                credit_rate_usd=DEMO_CREDIT_RATE,
            ),
        ]
    )
    return build_dashboard(control_plane)


@pytest.fixture
def empty_console(control_plane: ControlPlane) -> DashboardService:
    """A tenant with nothing in it. Every view must survive this."""
    return build_dashboard(control_plane)


# -- tenant isolation -----------------------------------------------------


def test_the_console_only_ever_sees_its_own_tenant(
    console: DashboardService, settings: Settings
) -> None:
    """The dashboard is a read surface over tenant-scoped services; it must not
    become the place tenant isolation is first bypassed."""
    assert console.events()

    with build_control_plane(settings, tenant_id="acme") as acme:
        other = build_dashboard(acme)
        assert other.tenant_id == "acme"
        assert other.events() == []
        assert other.findings() == []
        assert other.runs() == []


def test_every_query_survives_an_empty_tenant(empty_console: DashboardService) -> None:
    """A fresh install renders the same views as a seeded one."""
    assert empty_console.findings() == []
    assert empty_console.runs() == []
    assert empty_console.domain_activity() == []
    assert empty_console.decision_breakdown() == {}
    assert empty_console.chain("nope") is None

    options = empty_console.filter_options()
    assert options.platforms == () and options.agents == ()

    overview = empty_console.analytics.overview()
    assert overview.total_events == 0
    assert overview.would_block_count == 0
    assert overview.unknown_domain_events == 0

    provenance = empty_console.cost_provenance()
    assert provenance.total_usd == 0.0
    assert provenance.coverage == 0.0, "no events must not divide by zero"


# -- the product story: three sources, one core ---------------------------


def test_the_console_shows_all_three_sources(console: DashboardService) -> None:
    """The product story: three integrations feeding one core."""
    assert set(console.analytics.events_by_source()) == {"synthetic", "mcp", "snowflake"}
    assert set(console.filter_options().sources) == {"synthetic", "mcp", "snowflake"}


def test_platform_and_source_are_different_questions(console: DashboardService) -> None:
    """`platform` is where the work happened; `source_adapter` is which
    integration told us. The synthetic adapter emits Snowflake-platform events,
    so conflating the two would overstate real Snowflake coverage - it would
    report Snowflake activity in runs that never came from Snowflake."""
    by_platform = console.analytics.events_by_platform()
    by_source = console.analytics.events_by_source()

    assert set(by_platform) != set(by_source)
    assert sum(by_platform.values()) == sum(by_source.values())

    # Far more events name the Snowflake platform than arrived from its adapter.
    assert by_platform["snowflake"] > by_source["snowflake"]


def test_a_synthetic_run_spans_platforms_but_has_one_source(
    console: DashboardService,
) -> None:
    """A forecast run queries Snowflake, calls an MCP tool and hits an HTTP API.
    That is one run, one source, several platforms - and the console must not
    file it under the Snowflake integration."""
    run = next(r for r in console.runs() if "clean" in r.correlation_id)
    assert run.sources == ("synthetic",)
    assert len(run.platforms) > 1
    assert "snowflake" in run.platforms


# -- overview metrics -----------------------------------------------------


def test_overview_counts_what_enforcement_would_have_stopped(
    console: DashboardService,
) -> None:
    overview = console.analytics.overview()
    assert 0 < overview.would_block_count <= overview.violation_count
    assert overview.would_block_count == sum(1 for f in console.findings() if f.would_block)


def test_overview_counts_unclassified_activity(console: DashboardService) -> None:
    overview = console.analytics.overview()
    expected = sum(1 for e in console.events() if e.data_domain == UNKNOWN_DATA_DOMAIN)
    assert overview.unknown_domain_events == expected > 0


def test_decision_breakdown_only_reports_decisions_in_play(
    console: DashboardService,
) -> None:
    breakdown = console.decision_breakdown()
    assert breakdown, "the demo has violations, so some decision must be represented"
    assert all(count > 0 for count in breakdown.values())
    assert sum(breakdown.values()) == len(console.findings())


# -- finding filters ------------------------------------------------------


def test_findings_are_worst_first(console: DashboardService) -> None:
    ranks = [f.severity.rank for f in console.findings()]
    assert ranks == sorted(ranks, reverse=True)


def test_findings_exclude_passing_results(console: DashboardService) -> None:
    """Passes prove a rule ran - essential in the audit trail, noise in a
    findings table."""
    assert all(f.result.violated for f in console.findings())
    assert len(console.findings()) < len(console.policy_results())


def test_filtering_by_source(console: DashboardService) -> None:
    """Filtering by the Snowflake *integration* yields only the run that came
    from it."""
    findings = console.findings(FindingFilter(sources=frozenset({"snowflake"})))
    assert findings
    assert all("snowflake" in f.sources for f in findings)
    assert {f.correlation_id for f in findings} == {SNOWFLAKE_RUN}


def test_filtering_by_platform_is_broader_than_by_source(
    console: DashboardService,
) -> None:
    """Because synthetic runs also touch the Snowflake platform, the platform
    filter legitimately returns more than the source filter."""
    by_platform = console.findings(FindingFilter(platforms=frozenset({"snowflake"})))
    by_source = console.findings(FindingFilter(sources=frozenset({"snowflake"})))

    assert all("snowflake" in f.platforms for f in by_platform)
    assert len(by_platform) > len(by_source)
    assert {f.correlation_id for f in by_source} < {f.correlation_id for f in by_platform}


def test_filtering_by_severity_and_policy(console: DashboardService) -> None:
    critical = console.findings(FindingFilter(severities=frozenset({Severity.CRITICAL})))
    assert critical and all(f.severity is Severity.CRITICAL for f in critical)

    read_only = console.findings(FindingFilter(policies=frozenset({"read_only_violation"})))
    assert read_only and all(f.policy_name == "read_only_violation" for f in read_only)


def test_filtering_by_would_block(console: DashboardService) -> None:
    blocked = console.findings(FindingFilter(would_block_only=True))
    assert blocked and all(f.would_block for f in blocked)
    assert len(blocked) < len(console.findings()), "not every finding is blocking"


def test_filtering_by_agent_and_domain(console: DashboardService) -> None:
    by_agent = console.findings(FindingFilter(agents=frozenset({"agt_forecast_weekly"})))
    assert by_agent and all(f.agent_id == "agt_forecast_weekly" for f in by_agent)

    by_domain = console.findings(FindingFilter(data_domains=frozenset({"hr"})))
    assert by_domain and all(f.data_domain == "hr" for f in by_domain)


def test_filters_combine_as_and(console: DashboardService) -> None:
    combined = console.findings(
        FindingFilter(
            sources=frozenset({"snowflake"}),
            severities=frozenset({Severity.CRITICAL}),
        )
    )
    assert combined
    assert all("snowflake" in f.sources and f.severity is Severity.CRITICAL for f in combined)


def test_a_filter_matching_nothing_returns_nothing(console: DashboardService) -> None:
    """It must not silently widen back to everything - an empty result is an
    answer, and a filter that quietly ignores itself is a lie."""
    assert console.findings(FindingFilter(platforms=frozenset({"databricks"}))) == []
    assert console.findings(FindingFilter(sources=frozenset({"databricks"}))) == []
    assert console.findings(FindingFilter(agents=frozenset({"nobody"}))) == []


def test_filter_options_come_from_the_data_not_the_enums(console: DashboardService) -> None:
    options = console.filter_options()
    assert "databricks" not in options.platforms, "never offer a filter that returns nothing"
    assert set(options.platforms) == {e.platform.value for e in console.events()}
    assert set(options.sources) == {e.source_adapter for e in console.events()}
    assert options.severities == tuple(sorted(set(options.severities), key=lambda s: -s.rank)), (
        "severities descend, so the worst is the first thing offered"
    )


# -- unknown vs unauthorized ----------------------------------------------


def test_unknown_domain_is_our_gap_not_the_agents(console: DashboardService) -> None:
    """The whole point of the distinction: the console must be able to say who
    has to act, and it is not the agent."""
    unknown = console.findings(FindingFilter(policies=frozenset({"unknown_data_domain"})))
    assert unknown
    for finding in unknown:
        assert finding.is_configuration_gap
        assert finding.attribution == "configuration gap"
        assert not finding.would_block


def test_unauthorized_domain_is_agent_conduct(console: DashboardService) -> None:
    scope = console.findings(FindingFilter(policies=frozenset({"unauthorized_data_domain"})))
    assert scope
    for finding in scope:
        assert not finding.is_configuration_gap
        assert finding.attribution == "agent conduct"
        assert finding.would_block


def test_the_two_domain_rules_never_fire_on_the_same_domain(
    console: DashboardService,
) -> None:
    """Preserves the Tollgate 2 invariant at the presentation boundary: an
    unclassified object must not also read as a scope breach."""
    unauthorized = {
        f.data_domain
        for f in console.findings(FindingFilter(policies=frozenset({"unauthorized_data_domain"})))
    }
    assert UNKNOWN_DATA_DOMAIN not in unauthorized


# -- runs -----------------------------------------------------------------


def test_runs_are_newest_first_and_cover_every_source(console: DashboardService) -> None:
    runs = console.runs()
    assert [r.started_at for r in runs] == sorted((r.started_at for r in runs), reverse=True)
    assert {s for r in runs for s in r.sources} == {"synthetic", "mcp", "snowflake"}


def test_run_filters(console: DashboardService) -> None:
    snowflake = console.runs(RunFilter(sources=frozenset({"snowflake"})))
    assert [r.correlation_id for r in snowflake] == [SNOWFLAKE_RUN]

    violating = console.runs(RunFilter(violations_only=True))
    assert violating and all(r.violation_count > 0 for r in violating)
    assert len(violating) < len(console.runs()), "the demo has clean runs too"

    blocking = console.runs(RunFilter(would_block_only=True))
    assert blocking and all(r.would_block_count > 0 for r in blocking)


def test_runs_filter_by_platform_and_agent(console: DashboardService) -> None:
    """Platform is the broader axis - a synthetic run touching Snowflake matches
    the platform filter but not the source filter."""
    by_platform = console.runs(RunFilter(platforms=frozenset({"snowflake"})))
    by_source = console.runs(RunFilter(sources=frozenset({"snowflake"})))
    assert len(by_platform) > len(by_source)
    assert all("snowflake" in r.platforms for r in by_platform)

    by_agent = console.runs(RunFilter(agents=frozenset({"agt_analyst_copilot"})))
    assert by_agent and all("agt_analyst_copilot" in r.agent_ids for r in by_agent)

    assert console.runs(RunFilter(platforms=frozenset({"databricks"}))) == []
    assert console.runs(RunFilter(agents=frozenset({"nobody"}))) == []


def test_evidence_values_of_any_shape_survive_enrichment(
    console: DashboardService,
) -> None:
    """`evidence` is dict[str, Any] by design, so a rule is free to put a scalar
    where another puts a list. Enrichment must not assume one shape."""
    findings = console.findings()
    assert findings
    for finding in findings:
        assert all(isinstance(r, str) for r in finding.resources)
        assert all(isinstance(t, str) for t in finding.tool_names)
        assert finding.occurrences >= 1


def test_a_scalar_evidence_value_is_normalised_to_a_list() -> None:
    """Rules are free to put a scalar where another puts a list - `tool_name` is
    a string, `tools` is a list. The console must render either without caring."""
    from agent_control_plane.services.dashboard import _string_list

    assert _string_list(None) == []
    assert _string_list("sales.read") == ["sales.read"]
    assert _string_list(["a", "b"]) == ["a", "b"]
    assert _string_list(("a", "b")) == ["a", "b"]
    assert _string_list(42) == ["42"]


def test_run_limit_applies_after_filtering(console: DashboardService) -> None:
    assert len(console.runs(limit=2)) == 2
    assert len(console.runs(RunFilter(sources=frozenset({"snowflake"})), limit=5)) == 1


def test_a_snowflake_run_is_multi_root(console: DashboardService) -> None:
    """ACCOUNT_USAGE has no run-envelope record, so the chain is flat. The
    console reports that shape rather than pretending there is a tree."""
    run = next(r for r in console.runs() if r.correlation_id == SNOWFLAKE_RUN)
    assert run.is_multi_root
    assert run.root_count == run.event_count
    assert run.sources == ("snowflake",)


def test_a_synthetic_run_has_a_single_root(console: DashboardService) -> None:
    run = next(r for r in console.runs() if "clean" in r.correlation_id)
    assert not run.is_multi_root
    assert run.root_count == 1


# -- trace ----------------------------------------------------------------


@pytest.mark.parametrize("fragment", ["run-2026-08-17-violation", "mcp-analyst", SNOWFLAKE_RUN])
def test_every_source_shape_walks_completely(console: DashboardService, fragment: str) -> None:
    """Synthetic run envelopes, MCP parent/child spans and flat Snowflake chains
    must all render, with no event unreachable."""
    run = next(r for r in console.runs() if fragment in r.correlation_id)
    chain = console.chain(run.correlation_id)
    assert chain is not None

    walked = chain.walk()
    assert len(walked) == len(chain.events), "walk must reach every event"
    assert {e.event_id for _, e in walked} == {e.event_id for e in chain.events}
    assert min(depth for depth, _ in walked) == 0


def test_run_findings_explain_that_run_only(console: DashboardService) -> None:
    findings = console.run_findings(SNOWFLAKE_RUN)
    assert findings
    assert all(f.correlation_id == SNOWFLAKE_RUN for f in findings)
    assert {f.policy_name for f in findings} == {
        "read_only_violation",
        "unauthorized_data_domain",
        "unknown_data_domain",
    }


def test_a_missing_run_is_none_not_an_error(console: DashboardService) -> None:
    assert console.chain("no-such-run") is None
    assert console.run_findings("no-such-run") == []


# -- data domains and cost ------------------------------------------------


def test_domain_activity_covers_classified_and_unknown(console: DashboardService) -> None:
    rows = {r.domain: r for r in console.domain_activity()}
    assert "sales" in rows and rows["sales"].is_classified
    assert UNKNOWN_DATA_DOMAIN in rows
    assert not rows[UNKNOWN_DATA_DOMAIN].is_classified

    assert [r.event_count for r in console.domain_activity()] == sorted(
        (r.event_count for r in console.domain_activity()), reverse=True
    )


def test_domain_activity_counts_agree_with_the_events(console: DashboardService) -> None:
    rows = console.domain_activity()
    assert sum(r.event_count for r in rows) == sum(1 for e in console.events() if e.data_domain)
    for row in rows:
        assert row.costed_event_count <= row.event_count


def test_hr_shows_both_activity_and_a_finding(console: DashboardService) -> None:
    hr = next(r for r in console.domain_activity() if r.domain == "hr")
    assert hr.event_count > 0
    assert hr.violation_count > 0


def test_cost_provenance_separates_measured_from_derived(
    console: DashboardService,
) -> None:
    """Snowflake USD is credits times a rate someone supplied. Presenting it as
    a measurement would be presenting an assumption as source truth."""
    provenance = console.cost_provenance()
    assert provenance.total_usd > 0
    assert provenance.has_derived_cost
    assert provenance.derived_sources == ("snowflake",)
    assert "snowflake" in provenance.sources_with_cost


def test_cost_coverage_is_honest_about_uncosted_events(
    console: DashboardService,
) -> None:
    provenance = console.cost_provenance()
    assert provenance.costed_events + provenance.uncosted_events == len(console.events())
    assert 0.0 < provenance.coverage <= 1.0
    assert provenance.uncosted_events > 0, "not every event carries a cost"


def test_snowflake_cost_is_absent_without_a_rate(control_plane: ControlPlane) -> None:
    """The same extract, no credit rate: no fabricated dollars, and the console
    reports the platform as uncosted rather than as free."""
    control_plane.ingestion.ingest(
        SnowflakeActivitySource(
            SNOWFLAKE_EXTRACT,
            tenant_id=control_plane.tenant_id,
            domains=control_plane.registry.data_domains,
        )
    )
    provenance = build_dashboard(control_plane).cost_provenance()
    assert provenance.total_usd == 0.0
    assert provenance.sources_without_cost == ("snowflake",)
    assert not provenance.has_derived_cost
