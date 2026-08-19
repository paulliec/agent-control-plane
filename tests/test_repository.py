from __future__ import annotations

from datetime import timedelta

import pytest

from agent_control_plane.models.enums import (
    ActionType,
    ActorType,
    Decision,
    EventType,
    Platform,
    Severity,
    Status,
)
from agent_control_plane.repositories.base import ControlPlaneRepository
from agent_control_plane.repositories.sqlite import SchemaVersionError, SQLiteRepository
from tests.conftest import FIXED_NOW, OTHER_TENANT, T0, TENANT, make_event, make_result


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

    loaded = repo.get_event(TENANT, "evt-full")
    assert loaded == event
    assert loaded is not None
    assert loaded.tenant_id == TENANT
    assert loaded.metadata["nested"]["rows"] == 42


def test_save_is_idempotent_by_event_id(repo: SQLiteRepository) -> None:
    events = [make_event(event_id=f"e{i}") for i in range(3)]
    assert repo.save_events(events) == 3
    assert repo.save_events(events) == 0
    assert repo.count_events(TENANT) == 3


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

    assert {e.event_id for e in repo.list_events(TENANT, correlation_id="run-2")} == {"b", "c"}
    assert {e.event_id for e in repo.list_events(TENANT, agent_id="agt_a")} == {"a", "c"}
    assert {e.event_id for e in repo.list_events(TENANT, platform="snowflake")} == {"b"}
    assert {e.event_id for e in repo.list_events(TENANT, since=T0 + timedelta(minutes=30))} == {"b"}
    assert len(repo.list_events(TENANT, limit=2)) == 2


def test_events_come_back_in_time_order(repo: SQLiteRepository) -> None:
    repo.save_events(
        [
            make_event(event_id="late", timestamp=T0 + timedelta(minutes=5)),
            make_event(event_id="early"),
        ]
    )
    assert [e.event_id for e in repo.list_events(TENANT)] == ["early", "late"]


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
    chain = repo.get_chain(TENANT, "run-1")
    assert chain is not None
    assert chain.tenant_id == TENANT
    assert [e.event_id for _, e in chain.walk()] == ["root", "child"]
    assert repo.get_chain(TENANT, "nope") is None


def test_correlation_ids_are_most_recent_first(repo: SQLiteRepository) -> None:
    repo.save_events(
        [
            make_event(event_id="a", correlation_id="old"),
            make_event(event_id="b", correlation_id="new", timestamp=T0 + timedelta(days=1)),
        ]
    )
    assert repo.list_correlation_ids(TENANT) == ["new", "old"]


# -- tenant isolation -----------------------------------------------------


def _two_tenants(repo: SQLiteRepository) -> None:
    repo.save_events(
        [
            make_event(event_id="shared-id", correlation_id="run-1"),
            make_event(event_id="shared-id", correlation_id="run-1", tenant_id=OTHER_TENANT),
        ]
    )


def test_the_same_event_id_can_exist_in_two_tenants(repo: SQLiteRepository) -> None:
    """event_id is only unique within a tenant — two customers' gateways will
    happily mint the same span id."""
    _two_tenants(repo)
    assert repo.count_events(TENANT) == 1
    assert repo.count_events(OTHER_TENANT) == 1

    ours = repo.get_event(TENANT, "shared-id")
    theirs = repo.get_event(OTHER_TENANT, "shared-id")
    assert ours is not None and theirs is not None
    assert ours.tenant_id == TENANT
    assert theirs.tenant_id == OTHER_TENANT


def test_reads_never_cross_the_tenant_boundary(repo: SQLiteRepository) -> None:
    _two_tenants(repo)
    assert {e.tenant_id for e in repo.list_events(TENANT)} == {TENANT}
    assert repo.list_correlation_ids(OTHER_TENANT) == ["run-1"]

    chain = repo.get_chain(TENANT, "run-1")
    assert chain is not None
    assert len(chain.events) == 1  # not 2, despite the shared correlation_id


def test_list_tenants_is_the_one_unscoped_read(repo: SQLiteRepository) -> None:
    _two_tenants(repo)
    assert repo.list_tenants() == sorted([TENANT, OTHER_TENANT])


def test_a_missing_tenant_reads_as_empty_not_as_everything(repo: SQLiteRepository) -> None:
    """The failure mode worth guarding: an unknown tenant filter silently
    degrading into 'no filter'."""
    _two_tenants(repo)
    assert repo.list_events("nobody") == []
    assert repo.count_events("nobody") == 0
    assert repo.get_chain("nobody", "run-1") is None


# -- policy results -------------------------------------------------------


def test_policy_results_roundtrip_and_filter(repo: SQLiteRepository) -> None:
    repo.save_policy_results(
        [
            make_result(result_id="r1", decision=Decision.DENY),
            make_result(result_id="r2", passed=True, evidence={}),
        ]
    )

    assert len(repo.list_policy_results(TENANT)) == 2
    violations = repo.list_policy_results(TENANT, violations_only=True)
    assert [r.result_id for r in violations] == ["r1"]
    assert violations[0].evidence == {"action": "write"}
    assert violations[0].severity is Severity.CRITICAL


def test_decision_and_enforced_survive_the_roundtrip(repo: SQLiteRepository) -> None:
    repo.save_policy_results([make_result(decision=Decision.REQUIRE_APPROVAL)])
    loaded = repo.list_policy_results(TENANT)[0]
    assert loaded.decision is Decision.REQUIRE_APPROVAL
    assert loaded.enforced is False
    assert loaded.would_block is True


def test_re_evaluation_replaces_rather_than_duplicates(repo: SQLiteRepository) -> None:
    repo.save_policy_results([make_result()])
    repo.save_policy_results([make_result()])
    assert len(repo.list_policy_results(TENANT)) == 1


def test_delete_policy_results(repo: SQLiteRepository) -> None:
    repo.save_policy_results([make_result(result_id="r1"), make_result(result_id="r2")])
    assert repo.delete_policy_results(TENANT, "run-1") == 2
    assert repo.list_policy_results(TENANT) == []


def test_delete_stops_at_the_tenant_boundary(repo: SQLiteRepository) -> None:
    repo.save_policy_results([make_result(), make_result(tenant_id=OTHER_TENANT)])
    assert repo.delete_policy_results(TENANT, "run-1") == 1
    assert len(repo.list_policy_results(OTHER_TENANT)) == 1


# -- schema ---------------------------------------------------------------


def test_schema_is_created_only_once(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "twice.db"
    first = SQLiteRepository(path)
    first.initialise()
    first.save_events([make_event()])
    first.close()

    second = SQLiteRepository(path)
    second.initialise()  # must not wipe anything
    assert second.count_events(TENANT) == 1
    second.close()


def test_an_older_schema_fails_loudly_rather_than_reading_wrong(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A v1 database has no tenant_id column. Reading it with v2 code would
    return rows that quietly belong to nobody."""
    path = tmp_path / "old.db"
    stale = SQLiteRepository(path)
    stale.initialise()
    stale.conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', '1')"
    )
    stale.conn.commit()
    stale.close()

    with pytest.raises(SchemaVersionError, match="schema v1"):
        SQLiteRepository(path).initialise()
