from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from agent_control_plane.models.enums import (
    ActionType,
    Decision,
    EventType,
    Platform,
    Severity,
    Status,
)
from agent_control_plane.models.event import AgentEvent, ExecutionChain, build_chain
from agent_control_plane.models.policy import PolicyReport
from tests.conftest import OTHER_TENANT, T0, TENANT, make_event, make_result


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_event(timestamp=datetime(2026, 8, 3, 6, 0))


def test_timestamps_normalised_to_utc() -> None:
    from datetime import timezone

    event = make_event(timestamp=T0.astimezone(timezone(timedelta(hours=-5))))
    assert event.timestamp.utcoffset() == timedelta(0)
    assert event.timestamp == T0


def test_tool_call_requires_tool_name() -> None:
    with pytest.raises(ValidationError, match="tool_name"):
        make_event(tool_name=None)


def test_failure_requires_an_error() -> None:
    with pytest.raises(ValidationError, match="error_code or error_message"):
        make_event(status=Status.FAILURE)

    ok = make_event(status=Status.FAILURE, error_code="ERP_429")
    assert ok.status is Status.FAILURE


def test_event_cannot_parent_itself() -> None:
    with pytest.raises(ValidationError, match="own parent"):
        make_event(parent_event_id="evt-1")


def test_unknown_field_rejected() -> None:
    # extra="forbid": a vendor field leaking into the core should be loud.
    with pytest.raises(ValidationError):
        make_event(snowflake_query_id="abc")


def test_events_are_immutable() -> None:
    event = make_event()
    with pytest.raises(ValidationError):
        event.status = Status.FAILURE  # type: ignore[misc]


def test_fingerprint_is_stable_and_content_sensitive() -> None:
    a = make_event()
    b = make_event(event_id="evt-other")  # id excluded from the fingerprint
    c = make_event(resource="OTHER.TABLE")
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()


def test_violation_requires_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        make_result(evidence={})


def test_severity_ordering() -> None:
    assert Severity.CRITICAL.rank > Severity.HIGH.rank > Severity.MEDIUM.rank
    worst = max([Severity.LOW, Severity.CRITICAL, Severity.MEDIUM], key=lambda s: s.rank)
    assert worst is Severity.CRITICAL


# -- chain reconstruction -------------------------------------------------


def _chain_events() -> list[AgentEvent]:
    """Human -> Agent A -> tool -> API, plus a handoff to Agent B."""
    root = make_event(
        event_id="e-root",
        event_type=EventType.AGENT_RUN_START,
        action=ActionType.START,
        tool_name=None,
        data_domain=None,
        actor_id="human@contoso.com",
    )
    tool = make_event(
        event_id="e-tool", parent_event_id="e-root", timestamp=T0 + timedelta(seconds=1)
    )
    api = make_event(
        event_id="e-api",
        parent_event_id="e-tool",
        event_type=EventType.DATA_ACCESS,
        tool_name=None,
        resource="https://api.example/thing",
        timestamp=T0 + timedelta(seconds=2),
        estimated_cost_usd=0.25,
    )
    handoff = make_event(
        event_id="e-handoff",
        parent_event_id="e-root",
        event_type=EventType.AGENT_HANDOFF,
        action=ActionType.DELEGATE,
        tool_name=None,
        timestamp=T0 + timedelta(seconds=3),
    )
    child = make_event(
        event_id="e-child",
        parent_event_id="e-handoff",
        agent_id="agt_writer",
        timestamp=T0 + timedelta(seconds=4),
        estimated_cost_usd=0.5,
    )
    return [api, child, root, handoff, tool]  # deliberately unordered


def test_chain_rebuilds_the_tree() -> None:
    chain = build_chain("run-1", _chain_events())

    assert [e.event_id for e in chain.roots] == ["e-root"]
    walked = chain.walk()
    assert [(d, e.event_id) for d, e in walked] == [
        (0, "e-root"),
        (1, "e-tool"),
        (2, "e-api"),
        (1, "e-handoff"),
        (2, "e-child"),
    ]
    assert chain.agent_ids == ("agt_reader", "agt_writer")
    assert chain.total_cost_usd == 0.75
    assert chain.started_at == T0


def test_orphaned_events_still_render_as_roots() -> None:
    """A truncated export shouldn't produce an empty tree."""
    orphan = make_event(event_id="e-orphan", parent_event_id="e-missing")
    chain = build_chain("run-1", [orphan])
    assert [e.event_id for e in chain.roots] == ["e-orphan"]
    assert len(chain.walk()) == 1


def test_walk_survives_a_cycle() -> None:
    a = make_event(event_id="a", parent_event_id="b")
    b = make_event(event_id="b", parent_event_id="a", timestamp=T0 + timedelta(seconds=1))
    chain = build_chain("run-1", [a, b])
    walked = chain.walk()
    assert len(walked) == 2
    assert {e.event_id for _, e in walked} == {"a", "b"}


def test_chain_flags_failure() -> None:
    events = [*_chain_events(), make_event(event_id="e-bad", status=Status.DENIED)]
    assert build_chain("run-1", events).failed is True
    assert build_chain("run-1", _chain_events()).failed is False


def test_platform_is_an_enum_not_a_free_string() -> None:
    with pytest.raises(ValidationError):
        make_event(platform="snowflake-prod-eu")
    assert make_event(platform="snowflake").platform is Platform.SNOWFLAKE


# -- tenancy --------------------------------------------------------------


def test_tenant_id_is_required_not_defaulted() -> None:
    """A tenant that can be forgotten is a tenant that will be forgotten."""
    payload = make_event().model_dump()
    del payload["tenant_id"]
    with pytest.raises(ValidationError, match="tenant_id"):
        AgentEvent.model_validate(payload)


def test_tenant_id_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        make_event(tenant_id="")


def test_fingerprints_differ_across_tenants() -> None:
    """Two tenants can legitimately run identical workloads. If the fingerprint
    ignored tenant, deduplication would silently merge their events."""
    ours = make_event()
    theirs = make_event(tenant_id=OTHER_TENANT)
    assert ours.tenant_id != theirs.tenant_id
    assert ours.fingerprint() != theirs.fingerprint()


def test_fingerprint_still_ignores_event_id_within_a_tenant() -> None:
    # Guards the above test against being satisfied by a fingerprint that just
    # hashes everything.
    assert make_event().fingerprint() == make_event(event_id="evt-other").fingerprint()


def test_mixed_tenant_chain_is_rejected() -> None:
    events = [make_event(event_id="mine"), make_event(event_id="theirs", tenant_id=OTHER_TENANT)]
    with pytest.raises(ValidationError, match="mixes tenants"):
        build_chain("run-1", events)


def test_chain_rejects_events_foreign_to_its_declared_tenant() -> None:
    """Stating the tenant explicitly must not override what the events say."""
    with pytest.raises(ValidationError, match="mixes tenants"):
        build_chain("run-1", [make_event()], tenant_id=OTHER_TENANT)


def test_chain_takes_its_tenant_from_the_events() -> None:
    chain = build_chain("run-1", [make_event(tenant_id=OTHER_TENANT)])
    assert chain.tenant_id == OTHER_TENANT


def test_chain_constructed_directly_is_also_guarded() -> None:
    """The guard lives on the model, not just on the build_chain helper."""
    with pytest.raises(ValidationError, match="mixes tenants"):
        ExecutionChain(
            tenant_id=TENANT,
            correlation_id="run-1",
            events=(make_event(tenant_id=OTHER_TENANT),),
        )


def test_empty_chain_is_rejected() -> None:
    """An empty chain has no tenant to belong to, so it cannot be built."""
    with pytest.raises(ValueError, match="empty chain"):
        build_chain("run-1", [])


# -- decision / enforcement scaffolding -----------------------------------


def test_results_default_to_allow_and_unenforced() -> None:
    result = make_result()
    assert result.decision is Decision.ALLOW
    assert result.enforced is False
    assert result.would_block is False


def test_would_block_needs_both_a_violation_and_a_blocking_decision() -> None:
    assert make_result(decision=Decision.DENY).would_block is True
    assert make_result(decision=Decision.REQUIRE_APPROVAL).would_block is True
    assert make_result(decision=Decision.ALLOW).would_block is False
    # A pass never blocks, whatever else is true of it.
    assert make_result(passed=True, evidence={}).would_block is False


def test_a_passing_result_cannot_carry_a_blocking_decision() -> None:
    with pytest.raises(ValidationError, match="passing result"):
        make_result(passed=True, evidence={}, decision=Decision.DENY)


def test_enforcement_cannot_be_claimed_while_observe_only() -> None:
    """The product observes. Until that changes, a result claiming it enforced
    something is a lie the audit trail must not be able to tell."""
    with pytest.raises(ValidationError, match="enforcement is not implemented"):
        make_result(enforced=True)


def test_report_would_block_selects_only_the_blocking_violations() -> None:
    report = PolicyReport(
        tenant_id=TENANT,
        correlation_id="run-1",
        results=(
            make_result(result_id="r1", decision=Decision.DENY),
            make_result(result_id="r2", decision=Decision.ALLOW),
            make_result(result_id="r3", passed=True, evidence={}),
        ),
    )
    assert [r.result_id for r in report.would_block] == ["r1"]
    assert [r.result_id for r in report.violations] == ["r1", "r2"]
