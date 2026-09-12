"""Service objects -> table rows.

Pure functions, no Streamlit import. Row shaping is the part of a dashboard
most likely to be quietly wrong — a mislabelled column or a fabricated number
reads as authoritative — so it lives here where it can be tested, and `app.py`
only arranges what these return.
"""

from __future__ import annotations

from typing import Any

from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.enums import Severity
from agent_control_plane.models.event import AgentEvent, ExecutionChain
from agent_control_plane.services.analytics import RunSummary
from agent_control_plane.services.dashboard import CostProvenance, DomainActivity, Finding

# Text markers rather than colour: this is a governance console, and severity
# must survive being pasted into a ticket or read by someone colour-blind.
SEVERITY_MARK: dict[Severity, str] = {
    Severity.CRITICAL: "CRITICAL",
    Severity.HIGH: "HIGH",
    Severity.MEDIUM: "MEDIUM",
    Severity.LOW: "LOW",
    Severity.INFO: "INFO",
}

_TREE_MARK: dict[str, str] = {
    "agent_run_start": "run",
    "agent_run_end": "end",
    "agent_handoff": "->",
    "tool_call": "tool",
    "data_access": "data",
    "llm_call": "llm",
    "approval": "ok?",
    "agent_step": "step",
}


def severity_label(severity: Severity | None) -> str:
    return SEVERITY_MARK[severity] if severity else "—"


def format_usd(value: float | None) -> str:
    """`None` is not zero. An event with no cost data must not read as free."""
    return "—" if value is None else f"${value:,.4f}"


def format_duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    return f"{ms / 60_000:.1f} min"


def run_rows(runs: list[RunSummary]) -> list[dict[str, Any]]:
    return [
        {
            "Started": run.started_at.strftime("%Y-%m-%d %H:%M"),
            "Run": run.correlation_id,
            "Agents": ", ".join(run.agent_ids),
            "Source": ", ".join(run.sources),
            "Platforms": ", ".join(run.platforms),
            "Events": run.event_count,
            "Status": run.status_label,
            "Duration": format_duration(run.duration_ms),
            # Runs always have a cost figure (zero when nothing reported one),
            # so this is a total, not a measurement claim.
            "Cost (est.)": format_usd(run.cost_usd),
            "Findings": run.violation_count,
            "Would block": run.would_block_count,
            "Max severity": severity_label(run.max_severity),
        }
        for run in runs
    ]


def finding_rows(findings: list[Finding]) -> list[dict[str, Any]]:
    return [
        {
            "Severity": severity_label(finding.severity),
            "Rule": finding.policy_name,
            # The column that keeps `unknown_data_domain` from reading as
            # misconduct. See `Finding.is_configuration_gap`.
            "Attribution": finding.attribution,
            "Would block": "yes" if finding.would_block else "no",
            "Agent": finding.agent_id or "—",
            "Run": finding.correlation_id,
            "Source": ", ".join(finding.sources),
            "Domain": finding.data_domain or "—",
            "Occurrences": finding.occurrences,
            "Explanation": finding.result.explanation,
        }
        for finding in findings
    ]


def domain_rows(rows: list[DomainActivity]) -> list[dict[str, Any]]:
    return [
        {
            "Data domain": row.domain,
            "Classified": "yes" if row.is_classified else "no — governance gap",
            "Events": row.event_count,
            "Agents": row.agent_count,
            "Findings": row.violation_count,
            "Cost (est.)": format_usd(row.cost_usd),
            # Without this a domain whose events mostly lack cost data looks
            # cheap rather than unmeasured.
            "Cost coverage": f"{row.costed_event_count}/{row.event_count} events",
        }
        for row in rows
    ]


def trace_rows(chain: ExecutionChain) -> list[dict[str, Any]]:
    """Flatten a chain for display, preserving tree depth as indentation.

    Uses `ExecutionChain.walk`, which already handles every shape the sources
    produce: a synthetic run envelope, MCP parent/child spans, and the flat
    multi-root chain Snowflake yields because `ACCOUNT_USAGE` has no
    "run started" record. No special-casing per source.
    """
    return [
        {
            "": "  " * depth + _TREE_MARK.get(event.event_type.value, event.event_type.value),
            "Time": event.timestamp.strftime("%H:%M:%S.%f")[:-3],
            "Agent": event.agent_id,
            "Action": event.action.value,
            "Tool / resource": event.tool_name or event.resource or "—",
            "Domain": event.data_domain or "—",
            "Status": event.status.value,
            "Cost (est.)": format_usd(event.estimated_cost_usd),
            "Platform": event.platform.value,
        }
        for depth, event in chain.walk()
    ]


def event_detail(event: AgentEvent) -> dict[str, Any]:
    """Normalized fields worth reading before anyone opens the raw metadata."""
    detail: dict[str, Any] = {
        "event_id": event.event_id,
        "timestamp": event.timestamp.isoformat(),
        "platform": event.platform.value,
        "source_adapter": event.source_adapter,
        "agent_id": event.agent_id,
        "event_type": event.event_type.value,
        "action": event.action.value,
        "status": event.status.value,
    }
    # Only include fields the source actually populated: a detail pane full of
    # "None" trains people to stop reading it.
    optional = {
        "agent_name": event.agent_name,
        "actor_id": event.actor_id,
        "actor_type": event.actor_type.value if event.actor_type else None,
        "tool_name": event.tool_name,
        "tool_server": event.tool_server,
        "resource": event.resource,
        "data_domain": event.data_domain,
        "error_code": event.error_code,
        "error_message": event.error_message,
        "duration_ms": event.duration_ms,
        "estimated_cost_usd": event.estimated_cost_usd,
        "model": event.model,
        "input_tokens": event.input_tokens,
        "output_tokens": event.output_tokens,
        "parent_event_id": event.parent_event_id,
        "session_id": event.session_id,
    }
    detail.update({k: v for k, v in optional.items() if v is not None})
    return detail


def cost_caveat(provenance: CostProvenance) -> str:
    """One sentence stating what the cost figures actually are.

    Rendered wherever a dollar total appears. Snowflake reports credits; the
    dollars are credits multiplied by a rate a caller supplied, which is an
    assumption wearing the units of a measurement.
    """
    parts: list[str] = []
    if provenance.has_derived_cost:
        named = ", ".join(provenance.derived_sources)
        parts.append(
            f"Cost for {named} is derived: credits reported by the source, "
            "multiplied by a rate supplied at ingest. It is not a figure the "
            "source reported in dollars."
        )
    if provenance.sources_without_cost:
        named = ", ".join(provenance.sources_without_cost)
        parts.append(f"No cost data from: {named}.")
    if provenance.uncosted_events:
        parts.append(
            f"{provenance.uncosted_events:,} of "
            f"{provenance.costed_events + provenance.uncosted_events:,} events "
            "carry no cost figure and contribute nothing to these totals."
        )
    return " ".join(parts) if parts else "All ingested events carry source-reported cost."


def domain_gap_note(rows: list[DomainActivity]) -> str | None:
    """Prose for the unclassified bucket, or None when there is nothing to say."""
    unknown = next((r for r in rows if r.domain == UNKNOWN_DATA_DOMAIN), None)
    if unknown is None:
        return None
    return (
        f"{unknown.event_count:,} event(s) touched objects no mapping rule classifies. "
        "This is a gap in our configuration, not agent misconduct — add a mapping "
        "in config/data_domains.yaml. Agents are not penalised for it: the "
        "unknown_data_domain rule is advisory and never blocks."
    )
