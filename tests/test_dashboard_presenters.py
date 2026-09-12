"""Row shaping for the console.

Pure functions, no Streamlit. The cases that matter are the ones where a
careless format would assert something untrue: a missing cost rendered as zero,
or a configuration gap rendered as misconduct.
"""

from __future__ import annotations

import pytest

from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.dashboard import presenters as fmt
from agent_control_plane.models.enums import ActionType, EventType, Platform, Severity, Status
from agent_control_plane.models.event import build_chain
from agent_control_plane.models.policy import PolicyResult
from agent_control_plane.services.dashboard import (
    AttentionItem,
    CostProvenance,
    DomainActivity,
    Finding,
    FindingFilter,
    SourceActivity,
)
from tests.conftest import make_event, make_result


def _finding(
    result: PolicyResult | None = None,
    *,
    data_domain: str | None = "sales",
    sources: tuple[str, ...] = ("synthetic",),
) -> Finding:
    return Finding(
        result=result if result is not None else make_result(),
        platforms=("local",),
        sources=sources,
        data_domain=data_domain,
        resources=(),
        tool_names=(),
        occurrences=1,
    )


# -- cost is never overstated ---------------------------------------------


def test_absent_cost_is_not_rendered_as_zero() -> None:
    """`None` means nobody reported a cost. Showing $0.0000 would assert the
    activity was free, which is a different and false claim."""
    assert fmt.format_usd(None) == "—"
    assert fmt.format_usd(0.0) == "$0.0000"


def test_cost_keeps_enough_precision_to_be_useful() -> None:
    assert fmt.format_usd(0.000731) == "$0.0007"
    assert fmt.format_usd(1234.5) == "$1,234.5000"


def test_the_cost_caveat_names_derived_sources() -> None:
    """Snowflake dollars are credits times a supplied rate. The console has to
    say so wherever a total appears."""
    note = fmt.cost_caveat(
        CostProvenance(
            total_usd=12.5,
            costed_events=80,
            uncosted_events=20,
            sources_with_cost=("snowflake", "synthetic"),
            sources_without_cost=("mcp",),
            derived_sources=("snowflake",),
        )
    )
    assert "snowflake" in note
    assert "derived" in note and "not a figure the source reported" in note
    assert "No cost data from: mcp" in note
    assert "20 of 100 events" in note


def test_the_caveat_says_so_when_nothing_is_derived() -> None:
    note = fmt.cost_caveat(
        CostProvenance(
            total_usd=1.0,
            costed_events=10,
            uncosted_events=0,
            sources_with_cost=("synthetic",),
            sources_without_cost=(),
            derived_sources=(),
        )
    )
    assert "source-reported" in note
    assert "derived" not in note


# -- unknown vs unauthorized ----------------------------------------------


def test_a_configuration_gap_is_labelled_as_ours() -> None:
    rows = fmt.finding_rows(
        [
            _finding(
                result=make_result(policy_name="unknown_data_domain", severity=Severity.MEDIUM),
                data_domain=UNKNOWN_DATA_DOMAIN,
            )
        ]
    )
    assert rows[0]["Attribution"] == "configuration gap"
    assert rows[0]["Would block"] == "no"


def test_a_scope_breach_is_labelled_as_agent_conduct() -> None:
    from agent_control_plane.models.enums import Decision

    rows = fmt.finding_rows(
        [
            _finding(
                result=make_result(
                    policy_name="unauthorized_data_domain",
                    severity=Severity.HIGH,
                    decision=Decision.DENY,
                ),
                data_domain="hr",
            )
        ]
    )
    assert rows[0]["Attribution"] == "agent conduct"
    assert rows[0]["Would block"] == "yes"


def test_the_domain_gap_note_blames_configuration_not_the_agent() -> None:
    note = fmt.domain_gap_note(
        [
            DomainActivity(
                domain=UNKNOWN_DATA_DOMAIN,
                event_count=3,
                agent_count=1,
                violation_count=1,
                cost_usd=0.0,
                costed_event_count=0,
            )
        ]
    )
    assert note is not None
    assert "configuration" in note and "not agent misconduct" in note
    assert "never blocks" in note


def test_no_gap_note_when_everything_is_classified() -> None:
    rows = [
        DomainActivity(
            domain="sales",
            event_count=5,
            agent_count=1,
            violation_count=0,
            cost_usd=1.0,
            costed_event_count=5,
        )
    ]
    assert fmt.domain_gap_note(rows) is None


def test_domain_rows_show_partial_cost_coverage() -> None:
    """A domain whose events mostly lack cost data must not look cheap."""
    rows = fmt.domain_rows(
        [
            DomainActivity(
                domain="sales",
                event_count=10,
                agent_count=2,
                violation_count=0,
                cost_usd=0.5,
                costed_event_count=3,
            )
        ]
    )
    assert rows[0]["Cost coverage"] == "3/10 events"
    assert rows[0]["Classified"] == "yes"


def test_the_unknown_domain_row_says_it_is_a_gap() -> None:
    rows = fmt.domain_rows(
        [
            DomainActivity(
                domain=UNKNOWN_DATA_DOMAIN,
                event_count=2,
                agent_count=1,
                violation_count=1,
                cost_usd=0.0,
                costed_event_count=0,
            )
        ]
    )
    assert "governance gap" in rows[0]["Classified"]


# -- trace renders every shape --------------------------------------------


def test_a_nested_chain_indents_by_depth() -> None:
    root = make_event(event_id="e0", event_type=EventType.AGENT_RUN_START, action=ActionType.START)
    child = make_event(event_id="e1", parent_event_id="e0")
    rows = fmt.trace_rows(build_chain("run-1", [root, child]))

    assert len(rows) == 2
    assert rows[0][""].startswith("run")
    assert rows[1][""].startswith("  "), "a child indents under its parent"


def test_a_flat_multi_root_chain_still_renders_every_event() -> None:
    """Snowflake reports queries, not run envelopes, so every event is a root.
    The trace must show a forest rather than dropping to one arbitrary tree."""
    events = [
        make_event(
            event_id=f"q{i}",
            platform=Platform.SNOWFLAKE,
            event_type=EventType.DATA_ACCESS,
            tool_name=None,
            resource=f"SALES.PUBLIC.T{i}",
        )
        for i in range(4)
    ]
    rows = fmt.trace_rows(build_chain("sf-run", events))

    assert len(rows) == 4
    assert all(not row[""].startswith(" ") for row in rows), "roots are all at depth 0"
    assert [row["Tool / resource"] for row in rows] == [f"SALES.PUBLIC.T{i}" for i in range(4)]


def test_trace_rows_fall_back_from_tool_to_resource() -> None:
    event = make_event(
        event_type=EventType.DATA_ACCESS, tool_name=None, resource="HR.PUBLIC.DIM_EMPLOYEE"
    )
    assert fmt.trace_rows(build_chain("r", [event]))[0]["Tool / resource"] == (
        "HR.PUBLIC.DIM_EMPLOYEE"
    )


def test_trace_rows_never_render_missing_cost_as_zero() -> None:
    event = make_event(estimated_cost_usd=None)
    assert fmt.trace_rows(build_chain("r", [event]))[0]["Cost (est.)"] == "—"


# -- event detail ---------------------------------------------------------


def test_event_detail_omits_fields_the_source_did_not_populate() -> None:
    """A pane full of nulls trains people to stop reading it - and Snowflake
    legitimately leaves token splits and actor type unset."""
    detail = fmt.event_detail(make_event(input_tokens=None, model=None))
    assert "input_tokens" not in detail
    assert "model" not in detail
    assert detail["event_id"] and detail["platform"] and detail["source_adapter"]


def test_event_detail_keeps_populated_optional_fields() -> None:
    detail = fmt.event_detail(make_event(model="claude-sonnet-5", input_tokens=10))
    assert detail["model"] == "claude-sonnet-5"
    assert detail["input_tokens"] == 10


# -- misc formatting ------------------------------------------------------


@pytest.mark.parametrize(
    ("ms", "expected"), [(None, "—"), (500, "500 ms"), (4180, "4.2 s"), (120_000, "2.0 min")]
)
def test_duration_formatting(ms: int | None, expected: str) -> None:
    assert fmt.format_duration(ms) == expected


def test_severity_is_text_not_colour() -> None:
    """Severity has to survive being pasted into a ticket."""
    assert fmt.severity_label(Severity.CRITICAL) == "CRITICAL"
    assert fmt.severity_label(None) == "—"


def test_empty_inputs_produce_empty_tables() -> None:
    assert fmt.run_rows([]) == []
    assert fmt.finding_rows([]) == []
    assert fmt.domain_rows([]) == []


def test_finding_rows_carry_the_explanation() -> None:
    """The row has to be readable without opening a drilldown."""
    rows = fmt.finding_rows([_finding(result=make_result(explanation="tried to write"))])
    assert rows[0]["Explanation"] == "tried to write"
    assert rows[0]["Severity"] == "CRITICAL"


def test_run_rows_separate_source_from_platform() -> None:
    from datetime import UTC, datetime

    from agent_control_plane.services.analytics import RunSummary

    run = RunSummary(
        correlation_id="run-1",
        started_at=datetime(2026, 8, 18, 6, 0, tzinfo=UTC),
        ended_at=datetime(2026, 8, 18, 6, 5, tzinfo=UTC),
        duration_ms=300_000,
        agent_ids=("agt_a",),
        initiated_by="someone",
        event_count=4,
        tool_call_count=2,
        cost_usd=1.5,
        ok=True,
        violation_count=0,
        max_severity=None,
        platforms=("local", "snowflake"),
        sources=("synthetic",),
        would_block_count=0,
        root_count=1,
    )
    row = fmt.run_rows([run])[0]
    assert row["Source"] == "synthetic"
    assert row["Platforms"] == "local, snowflake"
    assert row["Max severity"] == "—"


def test_status_uses_the_event_status_verbatim() -> None:
    event = make_event(status=Status.DENIED)
    assert fmt.trace_rows(build_chain("r", [event]))[0]["Status"] == "denied"


# -- attention and drill-down ---------------------------------------------


def _item(key: str = "critical", **kwargs: object) -> AttentionItem:
    defaults: dict[str, object] = {
        "key": key,
        "count": 4,
        "label": "critical findings",
        "detail": "why it matters",
        "criteria": FindingFilter(severities=frozenset({Severity.CRITICAL})),
    }
    defaults.update(kwargs)
    return AttentionItem(**defaults)  # type: ignore[arg-type]


def test_the_attention_line_leads_with_the_count() -> None:
    assert fmt.attention_line(_item()) == "4 critical findings"


def test_large_counts_are_thousands_separated() -> None:
    assert fmt.attention_line(_item(count=1234)) == "1,234 critical findings"


def test_drilldown_targets_the_findings_page() -> None:
    state = fmt.drilldown_state(_item())
    assert state[fmt.PAGE_KEY] == fmt.FINDINGS_PAGE
    assert state[fmt.FINDING_WIDGET_KEYS["severities"]] == ["critical"]


def test_drilldown_clears_the_filters_it_does_not_set() -> None:
    """Drilling into 'critical' after 'would block' must show critical
    findings, not the intersection. Every key is written, empties included."""
    state = fmt.drilldown_state(_item())

    assert state[fmt.FINDING_WIDGET_KEYS["would_block"]] is False
    assert state[fmt.FINDING_WIDGET_KEYS["policies"]] == []
    assert state[fmt.FINDING_WIDGET_KEYS["sources"]] == []
    assert state[fmt.FINDING_WIDGET_KEYS["agents"]] == []
    assert state[fmt.FINDING_WIDGET_KEYS["data_domains"]] == []
    assert set(fmt.FINDING_WIDGET_KEYS.values()) <= set(state)


def test_drilldown_carries_a_would_block_filter() -> None:
    state = fmt.drilldown_state(_item("would_block", criteria=FindingFilter(would_block_only=True)))
    assert state[fmt.FINDING_WIDGET_KEYS["would_block"]] is True
    assert state[fmt.FINDING_WIDGET_KEYS["severities"]] == []


def test_drilldown_carries_a_rule_filter() -> None:
    criteria = FindingFilter(policies=frozenset({"unregistered_agent"}))
    state = fmt.drilldown_state(_item("unregistered_agent", criteria=criteria))
    assert state[fmt.FINDING_WIDGET_KEYS["policies"]] == ["unregistered_agent"]


def test_drilldown_values_are_deterministic() -> None:
    """Session state feeds multiselect widgets; unstable ordering would make
    the same click render differently between runs."""
    criteria = FindingFilter(policies=frozenset({"b_rule", "a_rule"}))
    state = fmt.drilldown_state(_item("x", criteria=criteria))
    assert state[fmt.FINDING_WIDGET_KEYS["policies"]] == ["a_rule", "b_rule"]


def test_the_pending_key_is_not_a_widget_key() -> None:
    """It exists precisely because Streamlit refuses writes to widget keys
    after instantiation; colliding with one would defeat the mechanism."""
    assert fmt.PENDING_KEY not in set(fmt.FINDING_WIDGET_KEYS.values())
    assert fmt.PENDING_KEY != fmt.PAGE_KEY


# -- source mix -----------------------------------------------------------


def _source(name: str, count: int, *, generated: bool = False) -> SourceActivity:
    return SourceActivity(source=name, event_count=count, run_count=1, is_generated=generated)


def test_source_split_charts_generated_activity_separately() -> None:
    """143 generated events on the same axis as 9 real ones makes the real
    integration look like noise. Same numbers, two scales."""
    rows = [
        _source("synthetic", 143, generated=True),
        _source("mcp", 10),
        _source("snowflake", 9),
    ]
    integrations, generated = fmt.source_split(rows)

    assert integrations == {"mcp": 10, "snowflake": 9}
    assert generated == {"synthetic": 143}


def test_source_split_discards_nothing() -> None:
    rows = [_source("synthetic", 143, generated=True), _source("mcp", 10)]
    integrations, generated = fmt.source_split(rows)
    assert sum(integrations.values()) + sum(generated.values()) == 153


def test_the_mix_note_says_the_scenario_is_authored() -> None:
    note = fmt.source_mix_note([_source("synthetic", 143, generated=True), _source("snowflake", 9)])
    assert "snowflake (9)" in note
    assert "synthetic (143)" in note
    assert "not captured from a running system" in note
    assert "152 events are counted in every metric" in note
    assert "nothing is excluded" in note


def test_the_mix_note_handles_integrations_only() -> None:
    note = fmt.source_mix_note([_source("mcp", 10)])
    assert "Integration extracts: mcp (10)" in note
    assert "Generated demo scenario" not in note


def test_the_mix_note_survives_no_activity() -> None:
    assert fmt.source_mix_note([]) == "No ingested activity."
    assert fmt.source_split([]) == ({}, {})
