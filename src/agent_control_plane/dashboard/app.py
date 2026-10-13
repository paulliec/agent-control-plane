"""Agent Control Plane console.

    streamlit run src/agent_control_plane/dashboard/app.py

Presentation only. Every number comes from `AnalyticsService` or
`DashboardService`; this module contains no SQL, no policy logic, no domain
classification and no chain construction. If a view needs something the
services do not expose, the method belongs in the service — not here.

One tenant is selected at a time and the whole page is built from services
bound to it, so the console cannot become the place tenant isolation is first
bypassed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import streamlit as st

from agent_control_plane.bootstrap import ControlPlane, build_control_plane, build_dashboard
from agent_control_plane.config import Settings, load_settings
from agent_control_plane.dashboard import presenters as fmt
from agent_control_plane.demo import ensure_demo_data
from agent_control_plane.models.enums import Severity
from agent_control_plane.services.dashboard import (
    DashboardService,
    Finding,
    FindingFilter,
    RunFilter,
)

PAGES = ("Overview", "Runs", "Findings", "Trace", "Data domains & cost")

# What a visitor to the hosted demo is looking at. Short on purpose: the console
# already states its caveats where the numbers are (cost provenance on the
# domain page, would-block on the Overview), and a wall of disclaimers in the
# sidebar would only train people to skip them.
DEMO_NOTE = (
    "**Demo environment — synthetic and representative telemetry.** The "
    "synthetic scenario is generated demo data; the MCP and Snowflake records "
    "are representative modelled telemetry read from committed fixtures. "
    "Nothing here is connected to a live enterprise environment, and "
    "*would block* is evaluated enforcement intent, not inline blocking."
)


# -- wiring ---------------------------------------------------------------
#
# Deliberately *not* cached with `st.cache_resource`. Streamlit runs each rerun
# on a thread from its ScriptRunner pool, and a sqlite3 connection belongs to
# the thread that opened it — so a cached ControlPlane raises
# "SQLite objects created in a thread can only be used in that same thread" on
# the second interaction. Opening per rerun costs a local file handle and some
# idempotent `CREATE TABLE IF NOT EXISTS` DDL, which is nothing next to the
# render itself. The alternative — relaxing `check_same_thread` in the
# repository — would weaken a persistence safety property to suit one caller's
# threading model, which is the wrong direction for the dependency to point.


@contextmanager
def _open(settings: Settings, tenant_id: str) -> Iterator[ControlPlane]:
    control_plane = build_control_plane(settings, db_path=settings.db_path, tenant_id=tenant_id)
    try:
        yield control_plane
    finally:
        control_plane.close()


def _tenants(settings: Settings) -> list[str]:
    """Tenants with data. The one intentionally unscoped read in the system."""
    with _open(settings, settings.tenant_id) as control_plane:
        return control_plane.repository.list_tenants()


# -- shared widgets -------------------------------------------------------


def _metric_row(items: list[tuple[str, Any, str | None]]) -> None:
    for column, (label, value, help_text) in zip(st.columns(len(items)), items, strict=True):
        column.metric(label, value, help=help_text)


def _multiselect(label: str, options: tuple[str, ...], key: str) -> frozenset[str]:
    """Nothing selected means no filter, which is Streamlit's natural default
    and matches how the service reads an empty filter set."""
    return frozenset(st.multiselect(label, options, key=key))


def _no_data_notice(settings: Settings) -> None:
    st.info(
        "No events for this tenant yet. Seed the deterministic demo and reload:\n\n"
        "```\npython scripts/seed_demo.py --reset\n```\n\n"
        f"Reading from `{settings.db_path}`."
    )


# -- pages ----------------------------------------------------------------


def page_overview(console: DashboardService) -> None:
    st.subheader("Overview")
    overview = console.analytics.overview()

    _metric_row(
        [
            ("Events", f"{overview.total_events:,}", "Normalized AgentEvents across all sources"),
            ("Runs", f"{overview.total_runs:,}", "Distinct execution chains"),
            ("Agents", f"{overview.unique_agents:,}", None),
            ("Tool calls", f"{overview.total_tool_calls:,}", None),
            (
                "Findings",
                f"{overview.violation_count:,}",
                "Policy violations. Passing results are recorded too, but are not findings.",
            ),
            (
                "Cost (est.)",
                fmt.format_usd(overview.total_cost_usd),
                "Estimated. Some sources report credits, not dollars.",
            ),
        ]
    )

    _attention_section(console)

    st.divider()
    left, right = st.columns(2)

    with left:
        st.caption("Events by source — which integration reported the activity")
        integrations, generated = fmt.source_split(console.source_activity())
        if integrations:
            st.bar_chart(integrations, horizontal=True)
        if generated:
            st.caption("Generated demo scenario, on its own scale")
            st.bar_chart(generated, horizontal=True)
        st.caption(fmt.source_mix_note(console.source_activity()))
        st.caption(
            "Every source normalizes into the same `AgentEvent` and is governed "
            "by the same rules. There is one control plane, not one per vendor."
        )

    with right:
        st.caption("Events by platform — where the work physically happened")
        st.bar_chart(console.analytics.events_by_platform(), horizontal=True)
        st.caption(
            "A different question from source: one agent run can query "
            "Snowflake, call an MCP tool and hit an HTTP API, so platform "
            "counts exceed what the Snowflake integration itself reported."
        )

    st.divider()
    left, right = st.columns(2)
    with left:
        st.caption("Findings by rule")
        st.bar_chart(console.analytics.violations_by_policy(), horizontal=True)
    with right:
        st.caption("What enforcement would have done")
        st.bar_chart(console.decision_breakdown(), horizontal=True)
        st.caption(
            "Recorded intent only. `enforced` is rejected by the model today, so "
            "an audit trail can never claim something was blocked when it was not."
        )


def _attention_section(console: DashboardService) -> None:
    """Counts worth looking at, each a button onto the evidence.

    Drill-down is the smallest thing that works: write the Findings page's own
    widget keys into session state, then rerun. Streamlit initialises a widget
    from session state when the key is already set, so the Findings page builds
    itself pre-filtered with no routing layer and no second filter code path —
    the filter comes from `AttentionItem.criteria`, which is the same
    `FindingFilter` the page would have constructed by hand.
    """
    items = console.attention()
    with st.container(border=True):
        st.markdown("**Needs attention**")
        if not items:
            st.caption(
                "Nothing outstanding: no critical findings, no would-block "
                "findings, every agent registered, every object classified."
            )
            return

        for column, item in zip(st.columns(len(items)), items, strict=True):
            with column:
                st.metric(item.label.capitalize(), f"{item.count:,}", help=item.detail)
                if st.button(
                    "View findings",
                    key=f"attention_{item.key}",
                    width="stretch",
                ):
                    # Queued, not applied: see `presenters.PENDING_KEY`.
                    st.session_state[fmt.PENDING_KEY] = fmt.drilldown_state(item)
                    st.rerun()


def page_runs(console: DashboardService) -> None:
    st.subheader("Runs")
    options = console.filter_options()

    col1, col2, col3 = st.columns([2, 2, 3])
    with col1:
        sources = _multiselect("Source", options.sources, "runs_sources")
    with col2:
        platforms = _multiselect("Platform", options.platforms, "runs_platforms")
    with col3:
        agents = _multiselect("Agent", options.agents, "runs_agents")

    col1, col2 = st.columns(2)
    violations_only = col1.checkbox("With findings only", key="runs_violations")
    would_block_only = col2.checkbox("Would-block only", key="runs_block")

    runs = console.runs(
        RunFilter(
            sources=sources,
            platforms=platforms,
            agents=agents,
            violations_only=violations_only,
            would_block_only=would_block_only,
        )
    )
    if not runs:
        st.info("No runs match these filters.")
        return

    st.caption(f"{len(runs)} run(s)")
    st.dataframe(fmt.run_rows(runs), width="stretch", hide_index=True)

    multi_root = [r for r in runs if r.is_multi_root]
    if multi_root:
        st.caption(
            f"{len(multi_root)} run(s) have several root events. That is expected "
            "for sources with no run-envelope record — Snowflake's `ACCOUNT_USAGE` "
            "reports queries, not 'agent run started' — and the trace view renders "
            "them as a forest rather than forcing a single tree."
        )

    st.divider()
    selected = st.selectbox("Inspect a run", [r.correlation_id for r in runs], key="runs_selected")
    if selected:
        _render_trace(console, selected)


def page_findings(console: DashboardService) -> None:
    st.subheader("Findings")
    options = console.filter_options()

    col1, col2, col3 = st.columns(3)
    with col1:
        sources = _multiselect("Source", options.sources, "f_sources")
        agents = _multiselect("Agent", options.agents, "f_agents")
    with col2:
        policies = _multiselect("Rule", options.policies, "f_policies")
        domains = _multiselect("Data domain", options.data_domains, "f_domains")
    with col3:
        severity_names = st.multiselect(
            "Severity", [s.value for s in options.severities], key="f_sev"
        )
        would_block_only = st.checkbox("Would-block only", key="f_block")

    findings = console.findings(
        FindingFilter(
            sources=sources,
            agents=agents,
            policies=policies,
            data_domains=domains,
            severities=frozenset(Severity(name) for name in severity_names),
            would_block_only=would_block_only,
        )
    )
    if not findings:
        st.info("No findings match these filters.")
        return

    gaps = [f for f in findings if f.is_configuration_gap]
    conduct = [f for f in findings if not f.is_configuration_gap]
    _metric_row(
        [
            ("Findings", f"{len(findings):,}", None),
            ("Would block", f"{sum(1 for f in findings if f.would_block):,}", None),
            ("Agent conduct", f"{len(conduct):,}", "The agent exceeded its governance envelope"),
            (
                "Configuration gaps",
                f"{len(gaps):,}",
                "ACP lacks the configuration to judge this. Ours to fix, not the agent's.",
            ),
        ]
    )

    if gaps:
        st.info(
            "**Attribution matters.** `unauthorized_data_domain` means an agent "
            "touched a domain outside its allowlist. `unknown_data_domain` means "
            "*we* never wrote a mapping rule for the object it touched — nobody "
            "could have been allowlisted for it, so it is advisory and never "
            "blocks. The Attribution column separates the two."
        )

    st.dataframe(fmt.finding_rows(findings), width="stretch", hide_index=True)

    st.divider()
    labels = {f"{f.policy_name} · {f.correlation_id} · {f.agent_id or '—'}": f for f in findings}
    chosen = st.selectbox("Inspect a finding", list(labels), key="f_selected")
    if chosen:
        _render_finding(console, labels[chosen])


def _render_finding(console: DashboardService, finding: Finding) -> None:
    st.markdown(f"**{finding.policy_name}** — {finding.result.explanation}")
    _metric_row(
        [
            ("Severity", fmt.severity_label(finding.severity), None),
            ("Decision", finding.result.decision.value, "What a gateway would have done"),
            ("Would block", "yes" if finding.would_block else "no", None),
            ("Attribution", finding.attribution, None),
            ("Occurrences", f"{finding.occurrences:,}", "Grouped: one problem, not one per event"),
        ]
    )
    with st.expander("Evidence"):
        st.json(finding.result.evidence)
    st.caption("The run this finding came from:")
    _render_trace(console, finding.correlation_id, key_prefix="finding")


def page_trace(console: DashboardService) -> None:
    st.subheader("Trace")
    runs = console.runs()
    if not runs:
        st.info("No runs to trace.")
        return
    selected = st.selectbox("Run", [r.correlation_id for r in runs], key="trace_selected")
    if selected:
        _render_trace(console, selected, key_prefix="trace")


def _render_trace(console: DashboardService, correlation_id: str, key_prefix: str = "run") -> None:
    """Render one execution chain plus the findings it produced.

    Works for every source shape because `ExecutionChain.walk` does: a synthetic
    run envelope, MCP parent/child spans, and Snowflake's flat multi-root chain.
    """
    chain = console.chain(correlation_id)
    if chain is None:
        st.warning(f"No chain found for `{correlation_id}`.")
        return

    summary = console.analytics.run_summary(correlation_id)
    if summary is not None:
        _metric_row(
            [
                ("Events", f"{summary.event_count:,}", None),
                ("Status", summary.status_label, None),
                ("Duration", fmt.format_duration(summary.duration_ms), None),
                ("Cost (est.)", fmt.format_usd(summary.cost_usd), None),
                ("Findings", f"{summary.violation_count:,}", None),
                ("Would block", f"{summary.would_block_count:,}", None),
            ]
        )
        st.caption(
            f"Source: {', '.join(summary.sources)} · "
            f"Platforms: {', '.join(summary.platforms)} · "
            f"Agents: {', '.join(summary.agent_ids)}"
        )
        if summary.is_multi_root:
            st.caption(
                f"{summary.root_count} root events — this source reports no run "
                "envelope, so the chain is a forest rather than a single tree."
            )

    st.dataframe(fmt.trace_rows(chain), width="stretch", hide_index=True)

    findings = console.run_findings(correlation_id)
    if findings:
        st.markdown("**Findings from this run**")
        st.dataframe(fmt.finding_rows(findings), width="stretch", hide_index=True)
    else:
        st.caption("No findings for this run. Every rule still ran and recorded a pass.")

    with st.expander("Raw event detail and source metadata"):
        labels = {
            f"{e.timestamp.strftime('%H:%M:%S')} · {e.event_type.value} · "
            f"{e.tool_name or e.resource or e.event_id}": e
            for e in chain.events
        }
        chosen = st.selectbox("Event", list(labels), key=f"{key_prefix}_event")
        if chosen:
            event = labels[chosen]
            st.json(fmt.event_detail(event))
            st.caption(
                "Adapter-preserved source fields — kept verbatim so a column we "
                "have not modelled is never silently lost."
            )
            st.json(event.metadata or {"(no source metadata)": None})


def page_domains(console: DashboardService) -> None:
    st.subheader("Data domains & cost")
    rows = console.domain_activity()
    provenance = console.cost_provenance()

    _metric_row(
        [
            ("Domains seen", f"{len(rows):,}", None),
            (
                "Classified",
                f"{sum(1 for r in rows if r.is_classified):,}",
                "Declared in config/data_domains.yaml",
            ),
            (
                "Cost (est.)",
                fmt.format_usd(provenance.total_usd),
                "Estimated — see the note below",
            ),
            (
                "Cost coverage",
                f"{provenance.coverage:.0%}",
                "Share of events carrying any cost figure",
            ),
        ]
    )

    # Stated wherever a dollar figure appears. Snowflake's dollars are credits
    # times a supplied rate, and presenting that as measured would be a lie.
    st.warning(fmt.cost_caveat(provenance), icon=":material/info:")

    if not rows:
        st.info("No classified activity yet.")
        return

    gap_note = fmt.domain_gap_note(rows)
    if gap_note:
        st.info(gap_note)

    st.dataframe(fmt.domain_rows(rows), width="stretch", hide_index=True)

    st.divider()
    left, right = st.columns(2)
    with left:
        st.caption("Events by data domain")
        st.bar_chart(console.analytics.events_by_data_domain(), horizontal=True)
    with right:
        st.caption("Estimated cost by data domain")
        spend = console.analytics.cost_by_data_domain()
        if any(spend.values()):
            st.bar_chart(spend, horizontal=True)
        else:
            st.caption("No cost data for any domain.")


# -- entry point ----------------------------------------------------------


def _apply_pending_drilldown() -> None:
    """Serve a queued drill-down before a single widget is built this run.

    Streamlit initialises a widget from session state when its key is already
    set, so writing the Findings page's own filter keys here is enough to make
    it render pre-filtered — no routing layer, and no second copy of the filter
    logic. It must happen before the nav radio exists, hence first thing in
    `main`. See `presenters.PENDING_KEY`.
    """
    pending = st.session_state.pop(fmt.PENDING_KEY, None)
    if pending:
        st.session_state.update(pending)


def _bootstrap_demo(settings: Settings) -> None:
    """Make the hosted demo self-sufficient: no seed step, no credentials.

    The work is `demo.ensure_demo_data`, not anything defined here — the console
    does not own the demo scenario any more than it owns a SQL query. It is
    called on every rerun and does real work on at most the first one, because
    the check is a tenant-scoped `COUNT(*)` and a populated database is left
    untouched.

    Off (`ACP_DEMO_BOOTSTRAP=false`) means an empty console stays empty. That is
    the right answer for a deployment reading real telemetry, where no events is
    a fact worth seeing rather than a gap to paper over with synthetic rows.
    """
    if not settings.demo_bootstrap:
        return
    with st.spinner("Preparing the deterministic demo dataset…"):
        ensure_demo_data(settings)


def main() -> None:
    st.set_page_config(page_title="Agent Control Plane", layout="wide")
    _apply_pending_drilldown()
    settings = load_settings()

    st.sidebar.title("Agent Control Plane")
    st.sidebar.caption("Observability and governance for AI agents across platforms")

    _bootstrap_demo(settings)

    if not Path(settings.db_path).exists():
        st.title("Agent Control Plane")
        _no_data_notice(settings)
        return

    tenants = _tenants(settings) or [settings.tenant_id]
    tenant_id = st.sidebar.selectbox("Tenant", tenants, key="tenant")
    st.sidebar.caption(
        "Every query on this page is scoped to the selected tenant. Switching "
        "rebuilds the services rather than filtering a shared result set."
    )

    page = st.sidebar.radio("View", PAGES, key="page")
    st.sidebar.divider()
    st.sidebar.caption(f"Database: `{settings.db_path.name}`")
    st.sidebar.caption("Observe-only. Nothing here enforces or blocks.")
    if settings.demo_bootstrap:
        st.sidebar.caption(DEMO_NOTE)

    st.title("Agent Control Plane")

    with _open(settings, tenant_id) as control_plane:
        console = build_dashboard(control_plane)
        if not console.events():
            _no_data_notice(settings)
            return
        _PAGE_RENDERERS[page](console)


_PAGE_RENDERERS: dict[str, Callable[[DashboardService], None]] = {
    "Overview": page_overview,
    "Runs": page_runs,
    "Findings": page_findings,
    "Trace": page_trace,
    "Data domains & cost": page_domains,
}


main()
