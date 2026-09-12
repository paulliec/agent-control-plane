"""Snowflake activity adapter.

The theme throughout: three questions with three different answerers.
Snowflake knows what SQL ran and - natively, now - whether an agent invoked
it. Which *logical* agent is sometimes Snowflake's answer and sometimes only
the agent's own QUERY_TAG. USER_NAME is neither. These tests pin down where
each line sits, and what happens when nobody can answer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_control_plane.adapters.base import AdapterError, EventSource, NormalizationError
from agent_control_plane.adapters.snowflake import (
    SnowflakeActivitySource,
    SnowflakeAttributionError,
    SnowflakeTagError,
    parse_query_tag,
    read_agents_info,
    resolve_attribution,
    to_events,
)
from agent_control_plane.config import REPO_ROOT
from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.data_domain import DataDomainRegistry
from agent_control_plane.models.enums import ActionType, ActorType, EventType, Platform, Status
from agent_control_plane.models.event import AgentEvent
from agent_control_plane.registry import load_data_domains
from tests.conftest import OTHER_TENANT

EXTRACT = REPO_ROOT / "data" / "samples" / "snowflake_activity.jsonl"
RUN = "sf-forecast-2026-08-18"


@pytest.fixture
def shipped_domains() -> DataDomainRegistry:
    """The committed vocabulary — the same one the CLI wires in."""
    return load_data_domains(REPO_ROOT / "config" / "data_domains.yaml")


def _tag(**acp: object) -> str:
    return json.dumps({"acp": {"run": RUN, "agent": "agt_forecast_weekly", **acp}})


def _row(**overrides: object) -> dict[str, object]:
    """A minimal well-formed joined ACCOUNT_USAGE row."""
    base: dict[str, object] = {
        "QUERY_ID": "01b7c2a1-0000-8f21-0000-4a2500019001",
        "QUERY_TAG": _tag(),
        "USER_NAME": "SVC_FORECAST_AGENT",
        "USER_TYPE": "SERVICE",
        "ROLE_NAME": "ANALYTICS_AGENT_RW",
        "WAREHOUSE_NAME": "WH_ANALYTICS_XS",
        "QUERY_TYPE": "SELECT",
        "EXECUTION_STATUS": "SUCCESS",
        "START_TIME": "2026-08-18T06:00:11.640Z",
        "TOTAL_ELAPSED_TIME": 2260,
        "CREDITS_USED_CLOUD_SERVICES": 0.000731,
        "BASE_OBJECTS_ACCESSED": [
            {"objectName": "SALES_PROD.PUBLIC.FCT_ORDERS", "objectDomain": "Table"}
        ],
        "OBJECTS_MODIFIED": [],
    }
    base.update(overrides)
    return base


def _one(**overrides: object) -> AgentEvent:
    events = to_events(_row(**overrides))
    assert len(events) == 1
    return events[0]


# -- protocol -------------------------------------------------------------


def test_source_satisfies_the_protocol() -> None:
    assert isinstance(SnowflakeActivitySource(EXTRACT), EventSource)


# -- the core mapping -----------------------------------------------------


def test_maps_a_governed_read(shipped_domains: DataDomainRegistry) -> None:
    events = to_events(_row(QUERY_TAG=_tag(tool="sales.query_history")), domains=shipped_domains)
    assert len(events) == 1
    event = events[0]

    assert event.event_id == "01b7c2a1-0000-8f21-0000-4a2500019001"
    assert event.platform is Platform.SNOWFLAKE
    assert event.event_type is EventType.TOOL_CALL
    assert event.action is ActionType.READ
    assert event.tool_name == "sales.query_history"
    assert event.resource == "SALES_PROD.PUBLIC.FCT_ORDERS"
    assert event.data_domain == "sales"
    assert event.agent_id == "agt_forecast_weekly"
    assert event.actor_id == "SVC_FORECAST_AGENT"
    assert event.actor_type is ActorType.SERVICE
    assert event.correlation_id == RUN
    assert event.status is Status.SUCCESS
    assert event.duration_ms == 2260
    assert event.source_adapter == "snowflake"


def test_without_a_declared_tool_it_is_data_access_not_a_tool_call() -> None:
    """`tool_name` is the agent's claim. Absent the claim we do not invent one,
    and an event with no tool is data access, not a tool call."""
    event = _one()
    assert event.event_type is EventType.DATA_ACCESS
    assert event.tool_name is None


def test_source_identifiers_are_preserved_for_traceability() -> None:
    event = _one()
    assert event.metadata["query_id"] == "01b7c2a1-0000-8f21-0000-4a2500019001"
    assert event.metadata["role_name"] == "ANALYTICS_AGENT_RW"
    assert event.metadata["warehouse_name"] == "WH_ANALYTICS_XS"
    assert event.metadata["query_type"] == "SELECT"
    # The audit trail must say that agent identity came from a self-declared tag.
    assert event.metadata["attributed_by"] == "query_tag"


def test_unmodelled_columns_survive_in_metadata() -> None:
    """A column we haven't mapped yet must not vanish silently."""
    event = _one(BYTES_SPILLED_TO_LOCAL_STORAGE=4096, QUERY_HASH="abc123")
    assert event.metadata["BYTES_SPILLED_TO_LOCAL_STORAGE"] == 4096
    assert event.metadata["QUERY_HASH"] == "abc123"


def test_timestamps_become_utc_aware() -> None:
    event = _one()
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


# -- tenancy --------------------------------------------------------------


def test_tenant_is_carried_onto_every_event() -> None:
    events = to_events(_row(), tenant_id=OTHER_TENANT)
    assert {e.tenant_id for e in events} == {OTHER_TENANT}


def test_the_same_query_fingerprints_differently_per_tenant() -> None:
    """Existing fingerprint semantics must hold for a third source too."""
    mine = to_events(_row())[0]
    theirs = to_events(_row(), tenant_id=OTHER_TENANT)[0]
    assert mine.fingerprint() != theirs.fingerprint()


def test_fingerprints_are_stable_across_calls() -> None:
    assert to_events(_row())[0].fingerprint() == to_events(_row())[0].fingerprint()


# -- verbs ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("query_type", "action"),
    [
        ("SELECT", ActionType.READ),
        ("INSERT", ActionType.WRITE),
        ("UPDATE", ActionType.WRITE),
        ("MERGE", ActionType.WRITE),
        ("DELETE", ActionType.DELETE),
        ("TRUNCATE_TABLE", ActionType.DELETE),
        ("CALL", ActionType.INVOKE),
    ],
)
def test_query_type_maps_to_the_governance_verb(query_type: str, action: ActionType) -> None:
    assert _one(QUERY_TYPE=query_type).action is action


def test_a_modified_object_is_a_write_even_when_the_query_type_reads() -> None:
    """`CREATE TABLE x AS SELECT` reads and writes. The write is what governance
    needs to hear about."""
    event = _one(
        QUERY_TYPE="SELECT",
        BASE_OBJECTS_ACCESSED=[],
        OBJECTS_MODIFIED=[{"objectName": "INVENTORY.PUBLIC.DIM_SKU"}],
    )
    assert event.action is ActionType.WRITE


def test_an_unmappable_query_type_is_rejected_rather_than_guessed() -> None:
    """Guessing the verb means guessing whether an agent wrote to production."""
    with pytest.raises(ValueError, match="unsupported QUERY_TYPE"):
        to_events(_row(QUERY_TYPE="USE"))


# -- status ---------------------------------------------------------------


def test_an_rbac_refusal_is_denied_not_failure() -> None:
    """Same distinction the MCP adapter draws: policy said no is not a crash."""
    event = _one(
        EXECUTION_STATUS="FAIL",
        ERROR_CODE="003001",
        ERROR_MESSAGE="Insufficient privileges to operate on table",
    )
    assert event.status is Status.DENIED
    assert event.error_code == "003001"


def test_a_compilation_error_is_a_failure() -> None:
    event = _one(
        EXECUTION_STATUS="FAIL",
        ERROR_CODE="000904",
        ERROR_MESSAGE="invalid identifier",
    )
    assert event.status is Status.FAILURE


def test_a_failure_always_carries_a_reason() -> None:
    """AgentEvent rejects a failure with no error; the adapter must always
    supply one rather than let the model raise."""
    event = _one(EXECUTION_STATUS="FAIL")
    assert event.status is Status.FAILURE
    assert event.error_code == "SNOWFLAKE_ERROR"


# -- data domains ---------------------------------------------------------


def test_objects_classify_through_the_shared_vocabulary(
    shipped_domains: DataDomainRegistry,
) -> None:
    event = to_events(
        _row(BASE_OBJECTS_ACCESSED=[{"objectName": "HR.PUBLIC.DIM_EMPLOYEE"}]),
        domains=shipped_domains,
    )[0]
    assert event.data_domain == "hr"


def test_an_unmapped_object_becomes_the_unknown_domain(
    shipped_domains: DataDomainRegistry,
) -> None:
    event = to_events(
        _row(BASE_OBJECTS_ACCESSED=[{"objectName": "RAW_LANDING.PUBLIC.VENDOR_FEED"}]),
        domains=shipped_domains,
    )[0]
    assert event.data_domain == UNKNOWN_DATA_DOMAIN


def test_no_vocabulary_means_everything_is_unknown() -> None:
    """Unlike MCP there is no vendor-supplied domain to fall back on: Snowflake
    reports objects, not classifications. Without a vocabulary we cannot
    classify, and we say so rather than inventing a name from the database."""
    assert _one().data_domain == UNKNOWN_DATA_DOMAIN


def test_one_query_touching_two_domains_fans_out(
    shipped_domains: DataDomainRegistry,
) -> None:
    """Collapsing to a single event would mean picking one domain and silently
    dropping the other - here, losing an HR access entirely."""
    events = to_events(
        _row(
            BASE_OBJECTS_ACCESSED=[
                {"objectName": "SALES_PROD.PUBLIC.FCT_ORDERS"},
                {"objectName": "HR.PUBLIC.DIM_EMPLOYEE"},
            ]
        ),
        domains=shipped_domains,
    )
    assert [e.data_domain for e in events] == ["sales", "hr"]
    assert len({e.event_id for e in events}) == 2, "fanned-out ids must stay unique"
    assert {e.correlation_id for e in events} == {RUN}
    assert {e.metadata["query_id"] for e in events} == {_row()["QUERY_ID"]}


def test_a_table_both_read_and_written_is_reported_once_as_a_write() -> None:
    event = _one(
        BASE_OBJECTS_ACCESSED=[{"objectName": "INVENTORY.PUBLIC.DIM_SKU"}],
        OBJECTS_MODIFIED=[{"objectName": "INVENTORY.PUBLIC.DIM_SKU"}],
    )
    assert event.action is ActionType.WRITE


# -- cortex ---------------------------------------------------------------


CORTEX = {
    "FUNCTION_NAME": "SNOWFLAKE.CORTEX.COMPLETE",
    "MODEL_NAME": "claude-sonnet-5",
    "TOKENS": 9310,
    "TOKEN_CREDITS": 0.0186,
}


def test_a_cortex_row_is_an_llm_call() -> None:
    event = _one(CORTEX_FUNCTIONS=CORTEX, BASE_OBJECTS_ACCESSED=[])
    assert event.event_type is EventType.LLM_CALL
    assert event.action is ActionType.EXECUTE
    assert event.model == "claude-sonnet-5"


def test_a_single_token_total_is_not_split_into_input_and_output() -> None:
    """The source reports one TOKENS figure. Apportioning it across input and
    output would be arithmetic fiction, so both stay None and the total is
    preserved verbatim."""
    event = _one(CORTEX_FUNCTIONS=CORTEX, BASE_OBJECTS_ACCESSED=[])
    assert event.input_tokens is None
    assert event.output_tokens is None
    assert event.metadata["cortex_tokens_total"] == 9310


# -- cost -----------------------------------------------------------------


def test_credits_do_not_become_dollars_without_a_rate() -> None:
    """ACCOUNT_USAGE reports credits. The USD rate is contractual and is not in
    the telemetry, so absent one there is no honest cost to report."""
    event = _one()
    assert event.estimated_cost_usd is None
    assert event.metadata["credits_used_cloud_services"] == 0.000731


def test_a_supplied_rate_converts_credits_and_token_credits() -> None:
    event = to_events(
        _row(CORTEX_FUNCTIONS=CORTEX, BASE_OBJECTS_ACCESSED=[]), credit_rate_usd=3.00
    )[0]
    assert event.estimated_cost_usd == pytest.approx((0.000731 + 0.0186) * 3.00)


# -- attribution ----------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "match"),
    [
        ("", "no agent attribution"),
        ("run=x;agent=y", "not JSON"),
        ('["acp"]', "not a JSON object"),
        ('{"other": 1}', "no 'acp' object"),
        ('{"acp": {"run": "r"}}', "no agent id"),
        ('{"acp": {"agent": "a"}}', "no run id"),
    ],
)
def test_untrustworthy_tags_are_rejected(tag: str, match: str) -> None:
    with pytest.raises(SnowflakeTagError, match=match):
        parse_query_tag(tag)


def test_an_untagged_query_is_not_agent_activity() -> None:
    """A human running ad-hoc SQL has no agent id, and deriving one from the
    username would be inventing an agent that does not exist."""
    with pytest.raises(SnowflakeTagError):
        to_events(_row(QUERY_TAG="", USER_NAME="DANA.REYES", USER_TYPE="PERSON"))


def test_user_type_maps_to_actor_type_when_the_source_provides_it() -> None:
    assert _one(USER_TYPE="PERSON").actor_type is ActorType.HUMAN
    assert _one(USER_TYPE="SERVICE").actor_type is ActorType.SERVICE


def test_an_absent_user_type_leaves_actor_type_unknown() -> None:
    """Not every extract joins ACCOUNT_USAGE.USERS. Guessing human-vs-service
    from a name would be a fabrication, so the field stays null."""
    row = _row()
    del row["USER_TYPE"]
    assert to_events(row)[0].actor_type is None


def test_a_missing_query_id_is_rejected() -> None:
    row = _row()
    del row["QUERY_ID"]
    with pytest.raises(ValueError, match="QUERY_ID"):
        to_events(row)


def test_a_non_object_record_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be an object"):
        to_events(["not", "a", "row"])  # type: ignore[arg-type]


def test_an_unparseable_start_time_is_rejected() -> None:
    with pytest.raises(ValueError, match="unparseable START_TIME"):
        to_events(_row(START_TIME=1755500000))


# -- zero-event rows ------------------------------------------------------


def test_a_query_that_touched_nothing_yields_no_events() -> None:
    """`SELECT CURRENT_TIMESTAMP()` is well-formed and correctly tagged, but
    there is no object and therefore no governance question. Not an error."""
    assert to_events(_row(BASE_OBJECTS_ACCESSED=[], OBJECTS_MODIFIED=[])) == []


# -- the committed extract ------------------------------------------------


def test_reads_the_committed_extract(shipped_domains: DataDomainRegistry) -> None:
    events = list(SnowflakeActivitySource(EXTRACT, domains=shipped_domains).fetch())
    # 17 rows. Four are unmappable: not agentic (11009), USE (11012), and two
    # that are provably agentic but name no agent anywhere (11010, 11022).
    # One touches nothing; one fans out to two objects. 12 rows -> 13 events.
    assert len(events) == 13
    assert {e.source_adapter for e in events} == {"snowflake"}
    assert {e.platform for e in events} == {Platform.SNOWFLAKE}
    assert len({e.event_id for e in events}) == 13, "event ids must be unique"

    # Three runs now: the tagged ACP run, plus two natively-attributed queries
    # that carried no tag and are therefore each their own single-event run.
    assert RUN in {e.correlation_id for e in events}
    untagged = [e for e in events if e.correlation_id != RUN]
    assert {e.correlation_id for e in untagged} == {e.metadata["query_id"] for e in untagged}


def test_the_committed_extract_covers_the_governance_cases(
    shipped_domains: DataDomainRegistry,
) -> None:
    events = list(SnowflakeActivitySource(EXTRACT, domains=shipped_domains).fetch())
    domains = {e.data_domain for e in events}
    assert {"sales", "hr", "inventory", "reporting", UNKNOWN_DATA_DOMAIN} <= domains
    assert any(e.event_type is EventType.LLM_CALL for e in events)
    assert any(e.action is ActionType.WRITE for e in events)
    assert any(e.status is Status.DENIED for e in events)
    assert any(e.status is Status.FAILURE for e in events)


def test_since_filters_the_extract(shipped_domains: DataDomainRegistry) -> None:
    from datetime import UTC, datetime

    cutoff = datetime(2026, 8, 18, 6, 1, tzinfo=UTC)
    events = list(SnowflakeActivitySource(EXTRACT, domains=shipped_domains).fetch(since=cutoff))
    assert events and all(e.timestamp >= cutoff for e in events)


def test_bad_rows_are_skipped_by_default(tmp_path: Path) -> None:
    log = tmp_path / "extract.jsonl"
    log.write_text(
        "\n".join(
            [
                "# a comment",
                json.dumps(_row(QUERY_ID="ok-1")),
                json.dumps(_row(QUERY_ID="bad-1", QUERY_TAG="")),
                "",
                json.dumps(_row(QUERY_ID="ok-2")),
            ]
        ),
        encoding="utf-8",
    )
    events = list(SnowflakeActivitySource(log).fetch())
    assert [e.event_id for e in events] == ["ok-1", "ok-2"]


def test_strict_mode_raises_on_a_bad_row(tmp_path: Path) -> None:
    log = tmp_path / "extract.jsonl"
    log.write_text(json.dumps(_row(QUERY_TAG="")), encoding="utf-8")
    with pytest.raises(NormalizationError):
        list(SnowflakeActivitySource(log, strict=True).fetch())


def test_missing_file_is_an_adapter_error(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="not found"):
        list(SnowflakeActivitySource(tmp_path / "nope.jsonl").fetch())


# -- native agent attribution ---------------------------------------------
#
# Snowflake now reports QUERY_HISTORY.AGENT_TYPE and ACCESS_HISTORY.AGENTS_INFO.
# The old rule ("no tag, no event") is stale, but the replacement is not "native
# always wins" either: a native *type* with no native *name* still leaves the
# tag as the only identity, which is exactly the lite/external case.

CORTEX_AGENT_ID = "SALES_AI.PUBLIC.FORECAST_COPILOT"


def _agents_info(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    return list(entries)


def _cortex(**kw: Any) -> dict[str, Any]:
    return {
        "agentType": "CORTEX_AGENT",
        "agentId": CORTEX_AGENT_ID,
        "agentName": "Forecast Copilot",
        **kw,
    }


def _lite(**kw: Any) -> dict[str, Any]:
    return {"agentType": "CORTEX_LITE_AGENT", "agentId": None, "agentName": None, **kw}


def test_a_named_cortex_agent_supplies_its_own_identity() -> None:
    """Snowflake observed this name. It outranks a tag the agent wrote itself."""
    event = _one(AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))

    assert event.agent_id == CORTEX_AGENT_ID
    attribution = event.metadata["attribution"]
    assert attribution["agent_id_source"] == "snowflake_agents_info"
    assert attribution["agent_type"] == "CORTEX_AGENT"
    assert attribution["agent_type_source"] == "query_history.agent_type"


def test_native_identity_needs_no_query_tag() -> None:
    """The headline change. Snowflake alone is now enough to emit an event."""
    event = _one(QUERY_TAG="", AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))

    assert event.agent_id == CORTEX_AGENT_ID
    assert event.metadata["attribution"]["agent_id_source"] == "snowflake_agents_info"


def test_an_untagged_native_query_becomes_its_own_run() -> None:
    """There is no run envelope to join, and inventing one would group
    unrelated queries. The query id is the honest correlation id."""
    event = _one(QUERY_TAG="", AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))

    assert event.correlation_id == event.metadata["query_id"]
    assert event.metadata["attribution"]["correlation_id_source"] == "query_id"


def test_a_tagged_run_still_groups_natively_identified_queries() -> None:
    event = _one(AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))
    assert event.correlation_id == RUN
    assert event.metadata["attribution"]["correlation_id_source"] == "query_tag"


def test_a_lite_agent_proves_activity_without_proving_identity() -> None:
    """CORTEX_LITE_AGENT need not be a stable named agent. The type is real
    telemetry; the identity still has to come from the tag."""
    event = _one(AGENT_TYPE="CORTEX_LITE_AGENT", AGENTS_INFO=_agents_info(_lite()))

    assert event.agent_id == "agt_forecast_weekly"
    attribution = event.metadata["attribution"]
    assert attribution["agent_id_source"] == "query_tag"
    assert attribution["agent_type"] == "CORTEX_LITE_AGENT"
    assert attribution["native_agent_id"] is None


def test_an_external_agent_with_null_identity_falls_back_to_the_tag() -> None:
    """An agentic OAuth integration marks the session, not the agent."""
    external = {"agentType": "EXTERNAL_AGENT", "agentId": None, "agentName": None}
    event = _one(AGENT_TYPE="EXTERNAL_AGENT", AGENTS_INFO=_agents_info(external))

    assert event.agent_id == "agt_forecast_weekly"
    assert event.metadata["attribution"]["agent_id_source"] == "query_tag"
    assert event.metadata["attribution"]["agent_type"] == "EXTERNAL_AGENT"


def test_agentic_activity_with_no_identity_anywhere_is_dropped() -> None:
    """The honest failure. We know an agent did this and cannot say which, so
    we say nothing rather than pin it on the session user. This is the Q24 gap."""
    with pytest.raises(SnowflakeAttributionError, match="no resolvable agent identity"):
        to_events(
            _row(QUERY_TAG="", AGENT_TYPE="CORTEX_LITE_AGENT", AGENTS_INFO=_agents_info(_lite()))
        )


def test_the_drop_reason_names_the_agent_type_it_could_not_identify() -> None:
    """A drop that says which kind of agent got away is a measurable gap; a
    bare 'unattributable' is not."""
    with pytest.raises(SnowflakeAttributionError, match="CORTEX_LITE_AGENT"):
        to_events(_row(QUERY_TAG="", AGENT_TYPE="CORTEX_LITE_AGENT"))


def test_a_username_never_becomes_an_agent_id() -> None:
    """SERVICE_AGENT users and agentic OAuth mark a session as agentic. That is
    not a licence to promote USER_NAME into agent identity."""
    with pytest.raises(SnowflakeAttributionError):
        to_events(_row(QUERY_TAG="", AGENT_TYPE="EXTERNAL_AGENT", USER_NAME="SVC_SOME_AGENT"))


def test_a_non_agent_query_is_still_not_agent_activity() -> None:
    """Unchanged behaviour, now stated by Snowflake rather than inferred."""
    with pytest.raises(SnowflakeTagError, match="no agent attribution"):
        to_events(_row(QUERY_TAG="", AGENT_TYPE=None, AGENTS_INFO=None))


def test_agent_type_is_never_inferred_from_a_username() -> None:
    event = _one(USER_NAME="SVC_CORTEX_AGENT")
    assert event.metadata["attribution"]["agent_type"] is None
    assert event.metadata["attribution"]["agent_type_source"] is None


def test_agent_type_falls_back_to_the_nearest_agents_info_entry() -> None:
    """Some extracts join ACCESS_HISTORY without the QUERY_HISTORY column."""
    event = _one(AGENTS_INFO=_agents_info(_lite()))
    assert event.metadata["attribution"]["agent_type"] == "CORTEX_LITE_AGENT"
    assert event.metadata["attribution"]["agent_type_source"] == "snowflake_agents_info"


# -- actor stays separate from agent --------------------------------------


def test_actor_identity_never_collapses_into_agent_identity() -> None:
    """Two different questions, and the answer differs even when both exist."""
    event = _one(
        AGENT_TYPE="CORTEX_AGENT",
        AGENTS_INFO=_agents_info(_cortex()),
        USER_NAME="SVC_CORTEX_RUNTIME",
        USER_TYPE="SNOWFLAKE_SERVICE",
    )
    assert event.agent_id == CORTEX_AGENT_ID
    assert event.actor_id == "SVC_CORTEX_RUNTIME"
    assert event.actor_type is ActorType.SYSTEM
    assert event.metadata["attribution"]["actor_id_source"] == "snowflake_user"


# -- native and tag disagreeing -------------------------------------------


def test_both_identities_survive_when_they_disagree() -> None:
    """Neither value is discarded. The namespaces differ by nature, so this is
    the ordinary case rather than an anomaly - and no rule reads it."""
    event = _one(AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))
    attribution = event.metadata["attribution"]

    assert attribution["identity_agreement"] == "differ"
    assert attribution["native_agent_id"] == CORTEX_AGENT_ID
    assert attribution["query_tag_agent_id"] == "agt_forecast_weekly"
    assert event.agent_id == CORTEX_AGENT_ID, "native wins, but the tag is still on record"


def test_agreement_is_recorded_when_both_sources_match() -> None:
    matching = {"agentType": "EXTERNAL_AGENT", "agentId": "agt_forecast_weekly"}
    event = _one(AGENT_TYPE="EXTERNAL_AGENT", AGENTS_INFO=_agents_info(matching))
    assert event.metadata["attribution"]["identity_agreement"] == "agree"


def test_a_single_source_says_so_rather_than_claiming_agreement() -> None:
    assert _one().metadata["attribution"]["identity_agreement"] == "only_query_tag"

    native_only = _one(QUERY_TAG="", AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))
    assert native_only.metadata["attribution"]["identity_agreement"] == "only_native"


# -- the AGENTS_INFO chain ------------------------------------------------


def test_the_agents_info_chain_is_preserved_in_order() -> None:
    """Nearest invoker first, top-level last. The order is the meaning."""
    chain = _agents_info(_lite(), _cortex())
    event = _one(AGENT_TYPE="CORTEX_LITE_AGENT", AGENTS_INFO=chain)

    stored = event.metadata["snowflake_agents_info"]
    assert [e["agentType"] for e in stored] == ["CORTEX_LITE_AGENT", "CORTEX_AGENT"]
    assert stored == chain


def test_identity_comes_from_the_nearest_entry_that_has_one() -> None:
    """A lite agent invoked by a named one attributes to the named one, and
    records how far out it had to look."""
    event = _one(AGENT_TYPE="CORTEX_LITE_AGENT", AGENTS_INFO=_agents_info(_lite(), _cortex()))

    assert event.agent_id == CORTEX_AGENT_ID
    assert event.metadata["attribution"]["native_agent_index"] == 1
    # The direct invoker is still the lite agent; identity and type differ.
    assert event.metadata["attribution"]["agent_type"] == "CORTEX_LITE_AGENT"


def test_the_chain_is_not_turned_into_execution_edges() -> None:
    """AGENTS_INFO is attribution context for one query, not observed ACP
    events. Minting parent edges from it would invent a call tree."""
    event = _one(AGENT_TYPE="CORTEX_LITE_AGENT", AGENTS_INFO=_agents_info(_lite(), _cortex()))
    assert event.parent_event_id is None


def test_a_malformed_agents_info_is_ignored_not_fatal() -> None:
    assert read_agents_info(None) == ()
    assert read_agents_info("CORTEX_AGENT") == ()
    assert read_agents_info([{"agentType": "CORTEX_AGENT"}, "junk"]) == (
        {"agentType": "CORTEX_AGENT"},
    )


def test_an_empty_chain_leaves_no_metadata_key() -> None:
    """An empty array should not litter every event with a dead key."""
    assert "snowflake_agents_info" not in _one().metadata


# -- QUERY_TAG keeps earning its place ------------------------------------


def test_the_tag_still_supplies_the_run_and_the_tool() -> None:
    """Native telemetry says nothing about ACP runs or logical tool names."""
    event = _one(
        QUERY_TAG=_tag(tool="sales.query_history", step="pull-orders"),
        AGENT_TYPE="CORTEX_AGENT",
        AGENTS_INFO=_agents_info(_cortex()),
    )
    assert event.correlation_id == RUN
    assert event.tool_name == "sales.query_history"
    assert event.event_type is EventType.TOOL_CALL
    assert event.metadata["agent_step"] == "pull-orders"


def test_resolve_attribution_is_deterministic() -> None:
    row = _row(AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))
    assert resolve_attribution(row, "q1") == resolve_attribution(row, "q1")


# -- regressions the attribution change must not cause --------------------


def test_native_attribution_does_not_disturb_cost() -> None:
    """Q9 behaviour is untouched: credits become dollars only with a rate."""
    row = _row(AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))
    assert to_events(row)[0].estimated_cost_usd is None
    assert to_events(row, credit_rate_usd=3.0)[0].estimated_cost_usd == pytest.approx(0.002193)


def test_native_attribution_does_not_disturb_fan_out() -> None:
    events = to_events(
        _row(
            AGENT_TYPE="CORTEX_AGENT",
            AGENTS_INFO=_agents_info(_cortex()),
            BASE_OBJECTS_ACCESSED=[
                {"objectName": "SALES_PROD.PUBLIC.FCT_ORDERS"},
                {"objectName": "HR.PUBLIC.DIM_EMPLOYEE"},
            ],
        )
    )
    assert len(events) == 2
    assert all(e.agent_id == CORTEX_AGENT_ID for e in events)


def test_native_attribution_keeps_fingerprints_stable() -> None:
    """agent_id feeds the fingerprint, so a changed attribution path would
    silently re-key events. Same inputs must give the same fingerprint."""
    row = _row(AGENT_TYPE="CORTEX_AGENT", AGENTS_INFO=_agents_info(_cortex()))
    assert to_events(row)[0].fingerprint() == to_events(row)[0].fingerprint()
    # And a different attribution source really is a different event identity.
    assert to_events(row)[0].fingerprint() != _one().fingerprint()


def test_the_shipped_extract_exercises_every_attribution_path(
    shipped_domains: DataDomainRegistry,
) -> None:
    events = list(SnowflakeActivitySource(EXTRACT, domains=shipped_domains).fetch())
    sources = {e.metadata["attribution"]["agent_id_source"] for e in events}
    types = {e.metadata["attribution"]["agent_type"] for e in events}

    assert sources == {"query_tag", "snowflake_agents_info"}
    assert {"EXTERNAL_AGENT", "CORTEX_AGENT", "CORTEX_LITE_AGENT"} <= types
    assert any(e.metadata.get("snowflake_agents_info") for e in events)
