from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentops.adapters.base import AdapterError, EventSource, NormalizationError
from agentops.adapters.mcp import MCPCallLogSource, to_event
from agentops.config import REPO_ROOT
from agentops.models.enums import ActionType, ActorType, EventType, Platform, Status

SAMPLE_LOG = REPO_ROOT / "data" / "samples" / "mcp_tool_calls.jsonl"


def _record(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "ts": "2026-08-06T14:20:03.410Z",
        "trace_id": "mcp-run-1",
        "session_id": "sess-1",
        "span_id": "s1",
        "server": "snowflake-mcp",
        "method": "tools/call",
        "op": "read",
        "params": {"name": "sales.query_history", "arguments": {"weeks": 4}},
        "client": {
            "agent_id": "agt_analyst_copilot",
            "agent_name": "Analyst Copilot",
            "principal": "dana.reyes@contoso.com",
            "principal_type": "human",
        },
        "data_domain": "sales",
        "duration_ms": 1840,
        "cost_usd": 0.012,
        "result": {"isError": False},
    }
    base.update(overrides)
    return base


def test_source_satisfies_the_protocol() -> None:
    assert isinstance(MCPCallLogSource(SAMPLE_LOG), EventSource)


def test_maps_a_tool_call() -> None:
    event = to_event(_record())
    assert event.event_id == "s1"
    assert event.platform is Platform.MCP
    assert event.event_type is EventType.TOOL_CALL
    assert event.action is ActionType.READ
    assert event.tool_name == "sales.query_history"
    assert event.tool_server == "snowflake-mcp"
    assert event.agent_id == "agt_analyst_copilot"
    assert event.actor_id == "dana.reyes@contoso.com"
    assert event.actor_type is ActorType.HUMAN
    assert event.correlation_id == "mcp-run-1"
    assert event.status is Status.SUCCESS
    assert event.source_adapter == "mcp"
    assert event.metadata["arguments"] == {"weeks": 4}
    assert event.metadata["mcp_method"] == "tools/call"


def test_timestamps_become_utc_aware() -> None:
    event = to_event(_record())
    assert event.timestamp.tzinfo is not None
    assert event.timestamp.utcoffset().total_seconds() == 0  # type: ignore[union-attr]


def test_missing_op_stays_neutral_rather_than_guessing_read() -> None:
    """Defaulting an unlabelled tool call to 'read' would understate risk; the
    tool registry decides whether it mutates."""
    record = _record()
    del record["op"]
    assert to_event(record).action is ActionType.INVOKE


def test_resources_read_maps_to_data_access() -> None:
    event = to_event(
        _record(
            method="resources/read",
            resource="SALES.PUBLIC.FCT_ORDERS",
            params={},
            parent_span_id="s0",
        )
    )
    assert event.event_type is EventType.DATA_ACCESS
    assert event.action is ActionType.READ
    assert event.resource == "SALES.PUBLIC.FCT_ORDERS"
    assert event.parent_event_id == "s0"
    assert event.tool_name is None


def test_sampling_maps_to_llm_call() -> None:
    event = to_event(
        _record(
            method="sampling/createMessage",
            params={},
            model="claude-sonnet-5",
            input_tokens=8420,
            output_tokens=910,
        )
    )
    assert event.event_type is EventType.LLM_CALL
    assert event.model == "claude-sonnet-5"
    assert event.input_tokens == 8420


def test_gateway_rejection_becomes_denied_not_failure() -> None:
    """Policy denial and infrastructure failure are different signals."""
    event = to_event(
        _record(error={"code": "TOOL_NOT_PERMITTED", "message": "read-only principal"})
    )
    assert event.status is Status.DENIED
    assert event.error_code == "TOOL_NOT_PERMITTED"


def test_tool_error_becomes_failure() -> None:
    event = to_event(_record(result={"isError": True}))
    assert event.status is Status.FAILURE
    assert event.error_code == "TOOL_ERROR"


def test_timeout_status() -> None:
    event = to_event(_record(status="timeout", result={}))
    assert event.status is Status.TIMEOUT


def test_unknown_keys_are_preserved_in_metadata() -> None:
    event = to_event(_record(warehouse="WH_XS", rows_scanned=42))
    assert event.metadata["warehouse"] == "WH_XS"
    assert event.metadata["rows_scanned"] == 42


def test_missing_span_id_gets_a_deterministic_one() -> None:
    record = _record()
    del record["span_id"]
    first, second = to_event(record), to_event(record)
    assert first.event_id == second.event_id
    assert len(first.event_id) == 36  # uuid5


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ({"method": "prompts/list"}, "unsupported MCP method"),
        ({"client": {}}, "client.agent_id"),
        ({"params": {}}, "params.name"),
        ({"trace_id": None, "session_id": None}, "trace_id/session_id"),
        ({"ts": 12345}, "unparseable timestamp"),
    ],
)
def test_unmappable_records_raise(mutation: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        to_event(_record(**mutation))


# -- file source ----------------------------------------------------------


def test_reads_the_committed_sample_log() -> None:
    events = list(MCPCallLogSource(SAMPLE_LOG).fetch())
    # 10 records, one of which (prompts/list) is deliberately unmappable.
    assert len(events) == 9
    assert {e.correlation_id for e in events} == {"mcp-analyst-2026-08-06"}
    assert {e.source_adapter for e in events} == {"mcp"}
    denied = [e for e in events if e.status is Status.DENIED]
    assert [e.tool_name for e in denied] == ["reporting.publish"]


def test_bad_records_are_skipped_by_default(tmp_path: Path) -> None:
    """One malformed row in an export must not sink the whole ingest."""
    log = tmp_path / "log.jsonl"
    log.write_text(
        "\n".join(
            [
                "# a comment",
                json.dumps(_record(span_id="ok-1")),
                json.dumps(_record(span_id="bad-1", method="prompts/list")),
                "",
                json.dumps(_record(span_id="ok-2")),
            ]
        ),
        encoding="utf-8",
    )
    events = list(MCPCallLogSource(log).fetch())
    assert [e.event_id for e in events] == ["ok-1", "ok-2"]


def test_strict_mode_raises_on_a_bad_record(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text(json.dumps(_record(method="prompts/list")), encoding="utf-8")
    with pytest.raises(NormalizationError):
        list(MCPCallLogSource(log, strict=True).fetch())


def test_missing_file_is_an_adapter_error(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="not found"):
        list(MCPCallLogSource(tmp_path / "nope.jsonl").fetch())
