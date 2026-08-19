from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta

import pytest

from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.enums import (
    ActionType,
    Decision,
    EventType,
    Severity,
    Status,
)
from agent_control_plane.models.event import AgentEvent, ExecutionChain, build_chain
from agent_control_plane.services.policy_engine import (
    Finding,
    PolicyContext,
    PolicyEngine,
    PolicyRule,
)
from agent_control_plane.services.policy_rules import (
    ApprovalRequiredRule,
    CostThresholdRule,
    ReadOnlyViolationRule,
    ToolAllowlistRule,
    ToolCallVolumeRule,
    UnauthorizedDataDomainRule,
    UnknownDataDomainRule,
    UnregisteredAgentRule,
    default_rules,
)
from tests.conftest import T0, make_event


def _findings(rule: PolicyRule, ctx: PolicyContext, *events: AgentEvent) -> list[Finding]:
    return list(rule.evaluate(build_chain("run-1", list(events)), ctx))


def test_all_default_rules_satisfy_the_protocol() -> None:
    for rule in default_rules():
        assert isinstance(rule, PolicyRule)


def test_duplicate_rule_names_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        PolicyEngine([ReadOnlyViolationRule(), ReadOnlyViolationRule()])


# -- unauthorized data domain --------------------------------------------


def test_unauthorized_domain_fires(ctx: PolicyContext) -> None:
    findings = _findings(
        UnauthorizedDataDomainRule(), ctx, make_event(data_domain="hr", tool_name="hr.read")
    )
    assert len(findings) == 1
    assert findings[0].evidence["data_domain"] == "hr"
    assert "hr" in findings[0].explanation


def test_allowed_domain_passes(ctx: PolicyContext) -> None:
    assert _findings(UnauthorizedDataDomainRule(), ctx, make_event(data_domain="sales")) == []


def test_agent_without_domain_allowlist_is_unrestricted(ctx: PolicyContext) -> None:
    event = make_event(agent_id="agt_open", data_domain="anything")
    assert _findings(UnauthorizedDataDomainRule(), ctx, event) == []


def test_an_unclassified_domain_is_not_the_agents_scope_violation(
    ctx: PolicyContext,
) -> None:
    """Nobody can be allowlisted for `unknown`, so charging the agent with a
    scope breach here would turn every classification gap of ours into a HIGH
    would-block finding against them. `unknown_data_domain` owns this case."""
    event = make_event(data_domain=UNKNOWN_DATA_DOMAIN, resource="erp://returns/rma")
    assert _findings(UnauthorizedDataDomainRule(), ctx, event) == []


# -- tool allowlist -------------------------------------------------------


def test_tool_not_on_allowlist_fires(ctx: PolicyContext) -> None:
    findings = _findings(ToolAllowlistRule(), ctx, make_event(tool_name="inventory.adjust_stock"))
    assert len(findings) == 1
    assert findings[0].evidence["tool_name"] == "inventory.adjust_stock"


def test_allowlist_only_applies_to_tool_calls(ctx: PolicyContext) -> None:
    event = make_event(event_type=EventType.DATA_ACCESS, tool_name=None, resource="s3://x")
    assert _findings(ToolAllowlistRule(), ctx, event) == []


# -- read only ------------------------------------------------------------


def test_read_only_fires_on_write_action(ctx: PolicyContext) -> None:
    findings = _findings(
        ReadOnlyViolationRule(),
        ctx,
        make_event(action=ActionType.WRITE, tool_name="sales.read"),
    )
    assert len(findings) == 1
    assert findings[0].evidence["detected_by"] == ["action"]


def test_read_only_fires_on_mutating_tool_even_when_action_looks_innocent(
    ctx: PolicyContext,
) -> None:
    """The agent claims 'invoke'; the registry knows the tool writes."""
    findings = _findings(
        ReadOnlyViolationRule(),
        ctx,
        make_event(action=ActionType.INVOKE, tool_name="sales.write"),
    )
    assert len(findings) == 1
    assert findings[0].evidence["detected_by"] == ["tool_registry"]


def test_read_only_fires_even_when_the_gateway_denied_it(ctx: PolicyContext) -> None:
    """A blocked attempt is still a governance finding."""
    event = make_event(
        action=ActionType.WRITE,
        status=Status.DENIED,
        error_code="TOOL_NOT_PERMITTED",
    )
    findings = _findings(ReadOnlyViolationRule(), ctx, event)
    assert len(findings) == 1
    assert findings[0].evidence["statuses"] == {"denied": 1}


def test_read_write_agent_may_write(ctx: PolicyContext) -> None:
    event = make_event(agent_id="agt_writer", action=ActionType.WRITE, tool_name="sales.write")
    assert _findings(ReadOnlyViolationRule(), ctx, event) == []


def test_read_only_rule_is_critical() -> None:
    assert ReadOnlyViolationRule().severity is Severity.CRITICAL


# -- approval -------------------------------------------------------------


def _write_event() -> AgentEvent:
    return make_event(
        event_id="e-write",
        agent_id="agt_writer",
        action=ActionType.WRITE,
        tool_name="sales.write",
    )


def test_missing_approval_fires(ctx: PolicyContext) -> None:
    findings = _findings(ApprovalRequiredRule(), ctx, _write_event())
    assert len(findings) == 1


def test_linked_approval_by_tool_passes(ctx: PolicyContext) -> None:
    approval = make_event(
        event_id="e-approve",
        agent_id="agt_writer",
        event_type=EventType.APPROVAL,
        action=ActionType.APPROVE,
        tool_name=None,
        metadata={"approves_tool": "sales.write"},
    )
    assert _findings(ApprovalRequiredRule(), ctx, _write_event(), approval) == []


def test_linked_approval_by_event_id_passes(ctx: PolicyContext) -> None:
    approval = make_event(
        event_id="e-approve",
        agent_id="agt_writer",
        event_type=EventType.APPROVAL,
        action=ActionType.APPROVE,
        tool_name=None,
        metadata={"approves_event_id": "e-write"},
    )
    assert _findings(ApprovalRequiredRule(), ctx, _write_event(), approval) == []


def test_unlinked_approval_does_not_count(ctx: PolicyContext) -> None:
    """An approval that names nothing approves nothing."""
    approval = make_event(
        event_id="e-approve",
        agent_id="agt_writer",
        event_type=EventType.APPROVAL,
        action=ActionType.APPROVE,
        tool_name=None,
        metadata={"note": "looked fine to me"},
    )
    assert len(_findings(ApprovalRequiredRule(), ctx, _write_event(), approval)) == 1


# -- cost -----------------------------------------------------------------


def test_cost_threshold_fires_per_agent(ctx: PolicyContext) -> None:
    events = [
        make_event(event_id="e1", estimated_cost_usd=0.7),
        make_event(event_id="e2", estimated_cost_usd=0.6),
        make_event(event_id="e3", agent_id="agt_writer", estimated_cost_usd=0.2),
    ]
    findings = _findings(CostThresholdRule(), ctx, *events)
    assert len(findings) == 1
    assert findings[0].agent_id == "agt_reader"
    assert findings[0].evidence["spend_usd"] == pytest.approx(1.3)
    assert findings[0].evidence["top_contributors"][0]["event_id"] == "e1"


def test_cost_under_ceiling_passes(ctx: PolicyContext) -> None:
    assert _findings(CostThresholdRule(), ctx, make_event(estimated_cost_usd=0.9)) == []


def test_unregistered_agent_falls_back_to_default_ceiling(ctx: PolicyContext) -> None:
    event = make_event(agent_id="agt_ghost", estimated_cost_usd=1.5)
    findings = _findings(CostThresholdRule(), ctx, event)
    assert len(findings) == 1
    assert findings[0].evidence["limit_usd"] == 1.00


# -- tool call volume -----------------------------------------------------


def test_excessive_tool_calls_fires(ctx: PolicyContext) -> None:
    events = [make_event(event_id=f"e{i}", timestamp=T0 + timedelta(seconds=i)) for i in range(5)]
    findings = _findings(ToolCallVolumeRule(), ctx, *events)
    assert len(findings) == 1
    assert findings[0].evidence["tool_call_count"] == 5
    assert findings[0].evidence["by_tool"] == {"sales.read": 5}
    assert findings[0].event_id is None  # run-level finding


def test_non_tool_events_do_not_count(ctx: PolicyContext) -> None:
    events = [
        make_event(
            event_id=f"e{i}",
            event_type=EventType.DATA_ACCESS,
            tool_name=None,
            timestamp=T0 + timedelta(seconds=i),
        )
        for i in range(10)
    ]
    assert _findings(ToolCallVolumeRule(), ctx, *events) == []


# -- unregistered agent ---------------------------------------------------


def test_unregistered_agent_fires_once_per_agent(ctx: PolicyContext) -> None:
    events = [
        make_event(event_id="e1", agent_id="agt_ghost"),
        make_event(event_id="e2", agent_id="agt_ghost", timestamp=T0 + timedelta(seconds=1)),
        make_event(event_id="e3", agent_id="agt_reader"),
    ]
    findings = _findings(UnregisteredAgentRule(), ctx, *events)
    assert len(findings) == 1
    assert findings[0].agent_id == "agt_ghost"
    assert findings[0].evidence["occurrences"] == 2


# -- grouping -------------------------------------------------------------


def test_repeated_offences_collapse_to_one_finding(ctx: PolicyContext) -> None:
    """A retry storm is one governance problem, not eight — but every event id
    stays in the evidence."""
    events = [
        make_event(
            event_id=f"e{i}",
            tool_name="inventory.read_shipments",
            timestamp=T0 + timedelta(seconds=i),
            status=Status.FAILURE if i < 4 else Status.SUCCESS,
            error_code="ERP_429" if i < 4 else None,
        )
        for i in range(5)
    ]
    findings = _findings(ToolAllowlistRule(), ctx, *events)

    assert len(findings) == 1
    assert findings[0].evidence["occurrences"] == 5
    assert findings[0].evidence["event_ids"] == ["e0", "e1", "e2", "e3", "e4"]
    assert findings[0].evidence["statuses"] == {"failure": 4, "success": 1}
    assert "5 times" in findings[0].explanation


def test_distinct_subjects_stay_separate(ctx: PolicyContext) -> None:
    events = [
        make_event(event_id="e1", tool_name="a.one"),
        make_event(event_id="e2", tool_name="b.two", timestamp=T0 + timedelta(seconds=1)),
    ]
    findings = _findings(ToolAllowlistRule(), ctx, *events)
    assert {f.evidence["tool_name"] for f in findings} == {"a.one", "b.two"}


def test_same_offence_by_two_agents_stays_separate(ctx: PolicyContext) -> None:
    events = [
        make_event(event_id="e1", tool_name="x.y"),
        make_event(
            event_id="e2",
            tool_name="x.y",
            agent_id="agt_writer",
            timestamp=T0 + timedelta(seconds=1),
        ),
    ]
    findings = _findings(ToolAllowlistRule(), ctx, *events)
    assert {f.agent_id for f in findings} == {"agt_reader", "agt_writer"}


def test_findings_anchor_to_the_first_occurrence(ctx: PolicyContext) -> None:
    events = [
        make_event(event_id="late", tool_name="x.y", timestamp=T0 + timedelta(minutes=5)),
        make_event(event_id="early", tool_name="x.y"),
    ]
    findings = _findings(ToolAllowlistRule(), ctx, *events)
    assert findings[0].event_id == "early"


# -- deny-all scope -------------------------------------------------------


def test_empty_allowlist_denies_every_tool(ctx: PolicyContext) -> None:
    """agt_locked is allowlist-mode with nothing on it: nothing is permitted."""
    findings = _findings(
        ToolAllowlistRule(), ctx, make_event(agent_id="agt_locked", tool_name="sales.read")
    )
    assert len(findings) == 1
    assert findings[0].evidence["allowed_tools"] == []
    assert "deny all" in findings[0].explanation


def test_empty_allowlist_denies_every_data_domain(ctx: PolicyContext) -> None:
    findings = _findings(
        UnauthorizedDataDomainRule(),
        ctx,
        make_event(agent_id="agt_locked", data_domain="sales"),
    )
    assert len(findings) == 1


# -- unknown data domain --------------------------------------------------
#
# This rule is about *our* classification gap, not the agent's behaviour: it is
# MEDIUM and its decision is ALLOW. Blocking an agent because we never wrote a
# mapping rule would punish the wrong party.


def test_unknown_domain_fires_when_nothing_mapped(ctx_with_vocabulary: PolicyContext) -> None:
    findings = _findings(
        UnknownDataDomainRule(),
        ctx_with_vocabulary,
        make_event(
            agent_id="agt_open",
            data_domain=UNKNOWN_DATA_DOMAIN,
            resource="erp://returns/rma_queue",
        ),
    )
    assert len(findings) == 1
    assert findings[0].evidence["reason"] == "no_mapping_rule"
    assert findings[0].evidence["resources"] == ["erp://returns/rma_queue"]
    assert "data_domains.yaml" in findings[0].explanation


def test_a_domain_outside_the_vocabulary_fires_with_a_different_reason(
    ctx_with_vocabulary: PolicyContext,
) -> None:
    """A typo like `inventry` is a config drift signal, not a missing mapping."""
    findings = _findings(
        UnknownDataDomainRule(),
        ctx_with_vocabulary,
        make_event(agent_id="agt_open", data_domain="inventry"),
    )
    assert len(findings) == 1
    assert findings[0].evidence["reason"] == "not_in_vocabulary"
    assert findings[0].evidence["data_domain"] == "inventry"
    assert "sales" in findings[0].evidence["known_domains"]


def test_a_declared_domain_passes(ctx_with_vocabulary: PolicyContext) -> None:
    assert (
        _findings(UnknownDataDomainRule(), ctx_with_vocabulary, make_event(data_domain="sales"))
        == []
    )


def test_no_vocabulary_means_nothing_to_be_unknown_against(ctx: PolicyContext) -> None:
    """Without a configured vocabulary the rule must stay silent rather than
    flag every event on the platform."""
    assert _findings(UnknownDataDomainRule(), ctx, make_event(data_domain="whatever")) == []


def test_an_event_touching_no_data_is_not_a_classification_gap(
    ctx_with_vocabulary: PolicyContext,
) -> None:
    event = make_event(data_domain=None, tool_name=None, event_type=EventType.AGENT_RUN_START)
    assert _findings(UnknownDataDomainRule(), ctx_with_vocabulary, event) == []


def test_unknown_domain_is_observed_not_blocked() -> None:
    rule = UnknownDataDomainRule()
    assert rule.severity is Severity.MEDIUM
    assert rule.decision is Decision.ALLOW


# -- engine ---------------------------------------------------------------


def test_engine_records_a_pass_for_every_clean_rule(ctx: PolicyContext) -> None:
    """'No violations' must be distinguishable from 'nobody checked'."""
    engine = PolicyEngine(default_rules())
    chain = build_chain("run-1", [make_event()])
    report = engine.evaluate(chain, ctx)

    assert report.violations == ()
    assert {r.policy_name for r in report.results} == {r.name for r in default_rules()}
    assert all(r.passed for r in report.results)


def test_engine_result_ids_are_stable_across_runs(ctx: PolicyContext) -> None:
    engine = PolicyEngine(default_rules())
    chain = build_chain("run-1", [make_event(action=ActionType.WRITE)])
    first = engine.evaluate(chain, ctx)
    second = engine.evaluate(chain, ctx)
    assert [r.result_id for r in first.results] == [r.result_id for r in second.results]


def test_engine_reports_max_severity(ctx: PolicyContext) -> None:
    engine = PolicyEngine(default_rules())
    chain = build_chain(
        "run-1", [make_event(action=ActionType.WRITE, data_domain="hr", tool_name="hr.read")]
    )
    report = engine.evaluate(chain, ctx)
    assert report.max_severity is Severity.CRITICAL
    assert {v.policy_name for v in report.violations} >= {
        "read_only_violation",
        "unauthorized_data_domain",
        "tool_not_allowlisted",
    }


def test_a_broken_rule_does_not_void_the_evaluation(ctx: PolicyContext) -> None:
    class Exploding:
        name = "exploding"
        severity = Severity.LOW
        description = "always throws"
        decision = Decision.ALLOW

        def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
            raise RuntimeError("boom")

    broken: PolicyRule = Exploding()
    engine = PolicyEngine([broken, ReadOnlyViolationRule()])
    report = engine.evaluate(build_chain("run-1", [make_event(action=ActionType.WRITE)]), ctx)

    assert [v.policy_name for v in report.violations] == ["read_only_violation"]
    assert "exploding" not in {r.policy_name for r in report.results}
