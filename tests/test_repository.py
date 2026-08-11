from __future__ import annotations

from datetime import timedelta

from agentops.models.enums import ActionType, ActorType, EventType, Platform, Severity, Status
from agentops.models.policy import PolicyResult
from agentops.repositories.base import ControlPlaneRepository
from agentops.repositories.sqlite import SQLiteRepository
from tests.conftest import FIXED_NOW, T0, make_event


def test_sqlite_repo_satisfies_the_protocol(repo: SQLiteRepository) -> None:
    assert isinstance(repo, ControlPlaneRepository)


def test_roundtrip_preserves_every_field(repo: SQLiteRepository) -> None:
    event = make_event(
        event_id="evt-full",
        actor_id="human@contoso.com",
        actor_type=ActorType.HUMAN,
        agent_name="Reader",
        tool_server="snowflake-mcp",
        resource="SALES.PUBLIC.FCT_ORDERS",
        status=Status.DENIED,
        error_code="TOOL_NOT_PERMITTED",
        error_message="nope",
        duration_ms=1234,
        estimated_cost_usd=0.0125,
        model="claude-sonnet-5",
        input_tokens=100,
        output_tokens=20,
        parent_event_id="evt-parent",
        session_id="sess-1",
        source_adapter="synthetic",
        ingested_at=FIXED_NOW,
        metadata={"nested": {"rows": 42}, "list": [1, 2]},
    )
    repo.save_events([event])

    loaded = repo.get_event("evt-full")
    assert loaded == event
    assert loaded is not None
    assert loaded.metadata["nested"]["rows"] == 42


def test_save_is_idempotent_by_event_id(repo: SQLiteRepository) -> None:
    events = [make_event(event_id=f"e{i}") for i in range(3)]
    assert repo.save_events(events) == 3
    assert repo.save_events(events) == 0
    assert repo.count_events() == 3


def test_empty_save_is_a_noop(repo: SQLiteRepository) -> None:
    assert repo.save_events([]) == 0


def test_filters(repo: SQLiteRepository) -> None:
    repo.save_events(
        [
            make_event(event_id="a", correlation_id="run-1", agent_id="agt_a"),
            make_event(
                event_id="b",
                correlation_id="run-2",
                agent_id="agt_b",
                platform=Platform.SNOWFLAKE,
                timestamp=T0 + timedelta(hours=1),
            ),
            make_event(event_id="c", correlation_id="run-2", agent_id="agt_a"),
        ]
    )

    assert {e.event_id for e in repo.list_events(correlation_id="run-2")} == {"b", "c"}
    assert {e.event_id for e in repo.list_events(agent_id="agt_a")} == {"a", "c"}
    assert {e.event_id for e in repo.list_events(platform="snowflake")} == {"b"}
    assert {e.event_id for e in repo.list_events(since=T0 + timedelta(minutes=30))} == {"b"}
    assert len(repo.list_events(limit=2)) == 2


def test_events_come_back_in_time_order(repo: SQLiteRepository) -> None:
    repo.save_events(
        [
            make_event(event_id="late", timestamp=T0 + timedelta(minutes=5)),
            make_event(event_id="early"),
        ]
    )
    assert [e.event_id for e in repo.list_events()] == ["early", "late"]


def test_get_chain(repo: SQLiteRepository) -> None:
    repo.save_events(
        [
            make_event(
                event_id="root",
                event_type=EventType.AGENT_RUN_START,
                action=ActionType.START,
                tool_name=None,
            ),
            make_event(event_id="child", parent_event_id="root"),
        ]
    )
    chain = repo.get_chain("run-1")
    assert chain is not None
    assert [e.event_id for _, e in chain.walk()] == ["root", "child"]
    assert repo.get_chain("nope") is None


def test_correlation_ids_are_most_recent_first(repo: SQLiteRepository) -> None:
    repo.save_events(
        [
            make_event(event_id="a", correlation_id="old"),
            make_event(event_id="b", correlation_id="new", timestamp=T0 + timedelta(days=1)),
        ]
    )
    assert repo.list_correlation_ids() == ["new", "old"]


# -- policy results -------------------------------------------------------


def _result(result_id: str = "r1", *, passed: bool = False) -> PolicyResult:
    return PolicyResult(
        result_id=result_id,
        policy_name="read_only_violation",
        severity=Severity.CRITICAL,
        passed=passed,
        correlation_id="run-1",
        event_id="evt-1",
        agent_id="agt_reader",
        explanation="tried to write",
        evidence={} if passed else {"action": "write"},
        evaluated_at=FIXED_NOW,
    )


def test_policy_results_roundtrip_and_filter(repo: SQLiteRepository) -> None:
    repo.save_policy_results([_result("r1"), _result("r2", passed=True)])

    assert len(repo.list_policy_results()) == 2
    violations = repo.list_policy_results(violations_only=True)
    assert [r.result_id for r in violations] == ["r1"]
    assert violations[0].evidence == {"action": "write"}
    assert violations[0].severity is Severity.CRITICAL


def test_re_evaluation_replaces_rather_than_duplicates(repo: SQLiteRepository) -> None:
    repo.save_policy_results([_result("r1")])
    repo.save_policy_results([_result("r1")])
    assert len(repo.list_policy_results()) == 1


def test_delete_policy_results(repo: SQLiteRepository) -> None:
    repo.save_policy_results([_result("r1"), _result("r2")])
    assert repo.delete_policy_results("run-1") == 2
    assert repo.list_policy_results() == []


def test_schema_is_created_only_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "twice.db"
    first = SQLiteRepository(path)
    first.initialise()
    first.save_events([make_event()])
    first.close()

    second = SQLiteRepository(path)
    second.initialise()  # must not wipe anything
    assert second.count_events() == 1
    second.close()
