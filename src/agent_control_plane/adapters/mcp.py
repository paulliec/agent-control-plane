"""MCP tool-activity adapter.

Reads a JSON-lines call log of the shape an MCP gateway/proxy emits and maps it
onto `AgentEvent`. It deliberately does not import an MCP SDK: the control plane
consumes *logs*, not live protocol traffic, so this stays testable offline and
works against any gateway that can be persuaded to write JSONL.

Expected record shape (unknown keys are preserved in ``metadata``)::

    {
      "ts": "2026-08-03T14:22:05.120Z",
      "span_id": "...", "parent_span_id": "...", "trace_id": "...",
      "session_id": "...",
      "server": "weather-mcp",
      "method": "tools/call",
      "params": {"name": "weather.get_forecast", "arguments": {...}},
      "client": {"agent_id": "...", "agent_name": "...",
                 "principal": "...", "principal_type": "human"},
      "op": "read",
      "resource": "...", "data_domain": "weather",
      "duration_ms": 812, "cost_usd": 0.002,
      "result": {"isError": false},
      "error": {"code": "...", "message": "..."}
    }
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_control_plane.adapters.base import AdapterError, normalize_all
from agent_control_plane.constants import DEFAULT_TENANT_ID, UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.data_domain import DataDomainRegistry
from agent_control_plane.models.enums import (
    ActionType,
    ActorType,
    EventType,
    Platform,
    Status,
)
from agent_control_plane.models.event import AgentEvent

ADAPTER_NAME = "mcp"

_NAMESPACE = uuid.UUID("6f1f0f16-6c1a-4f2e-8a4a-6d2a2f1a0002")

# MCP method -> (event_type, default action). Unmapped methods are dropped with
# a warning rather than guessed at; guessing here would corrupt governance.
_METHOD_MAP: dict[str, tuple[EventType, ActionType]] = {
    "tools/call": (EventType.TOOL_CALL, ActionType.INVOKE),
    "resources/read": (EventType.DATA_ACCESS, ActionType.READ),
    "resources/write": (EventType.DATA_ACCESS, ActionType.WRITE),
    "sampling/createMessage": (EventType.LLM_CALL, ActionType.EXECUTE),
}

# Gateway rejections are governance signal, not infrastructure noise: keep them
# distinct from ordinary failures.
_DENIED_CODES = {"TOOL_NOT_PERMITTED", "FORBIDDEN", "UNAUTHORIZED", "POLICY_DENIED"}


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str):
        raise ValueError(f"unparseable timestamp: {value!r}")
    text = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _status(record: dict[str, Any]) -> tuple[Status, str | None, str | None]:
    error = record.get("error") or {}
    code = error.get("code")
    message = error.get("message")
    if code:
        status = Status.DENIED if str(code).upper() in _DENIED_CODES else Status.FAILURE
        return status, str(code), message or str(code)
    if (record.get("result") or {}).get("isError"):
        return Status.FAILURE, "TOOL_ERROR", "tool returned isError=true"
    if record.get("status") == "timeout":
        return Status.TIMEOUT, "MCP_TIMEOUT", "no response from server"
    return Status.SUCCESS, None, None


_KNOWN_KEYS = {
    "ts",
    "timestamp",
    "span_id",
    "parent_span_id",
    "trace_id",
    "session_id",
    "server",
    "method",
    "params",
    "client",
    "op",
    "resource",
    "data_domain",
    "duration_ms",
    "cost_usd",
    "result",
    "error",
    "status",
    "model",
    "input_tokens",
    "output_tokens",
}


def classify_domain(record: dict[str, Any], domains: DataDomainRegistry | None) -> str | None:
    """Vendor object -> controlled governance domain.

    The gateway's own ``data_domain`` string is treated as a *claim*, not an
    answer: it only survives if it is in the controlled vocabulary. A resource
    with a mapping rule beats the claim, because mapping rules are ours and the
    claim is the vendor's.

    With no vocabulary configured there is nothing to validate against, so the
    record passes through unchanged — that keeps the adapter usable standalone
    without silently inventing classifications when a vocabulary *does* exist.
    """
    claimed = record.get("data_domain")
    if domains is None or not domains.domains:
        return claimed

    if resource := record.get("resource"):
        mapped = domains.classify(Platform.MCP, str(resource))
        if mapped != UNKNOWN_DATA_DOMAIN:
            return mapped

    if claimed and domains.is_known(str(claimed)):
        return str(claimed)

    # Something was touched but nobody classified it. Say so out loud.
    return UNKNOWN_DATA_DOMAIN if (claimed or record.get("resource")) else None


def to_event(
    record: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT_ID,
    domains: DataDomainRegistry | None = None,
) -> AgentEvent:
    """Map one raw MCP log record. Raises on anything it can't map honestly."""
    if not isinstance(record, dict):
        raise ValueError("record must be an object")

    method = record.get("method")
    if method not in _METHOD_MAP:
        raise ValueError(f"unsupported MCP method: {method!r}")
    event_type, default_action = _METHOD_MAP[method]

    client = record.get("client") or {}
    agent_id = client.get("agent_id")
    if not agent_id:
        raise ValueError("record is missing client.agent_id")

    params = record.get("params") or {}
    tool_name = params.get("name") if event_type is EventType.TOOL_CALL else None
    if event_type is EventType.TOOL_CALL and not tool_name:
        raise ValueError("tools/call record is missing params.name")

    timestamp = _parse_ts(record.get("ts") or record.get("timestamp"))
    correlation_id = record.get("trace_id") or record.get("session_id")
    if not correlation_id:
        raise ValueError("record is missing trace_id/session_id")

    # `op` is the gateway's declared intent. Absent it we stay neutral (INVOKE)
    # and let the tool registry decide whether this mutates anything.
    op = record.get("op")
    action = ActionType(op) if op else default_action

    status, error_code, error_message = _status(record)

    principal_type = client.get("principal_type")
    actor_type = ActorType(principal_type) if principal_type else None

    span_id = record.get("span_id") or str(
        uuid.uuid5(_NAMESPACE, f"{correlation_id}:{timestamp.isoformat()}:{tool_name or method}")
    )

    metadata: dict[str, Any] = {k: v for k, v in record.items() if k not in _KNOWN_KEYS}
    if arguments := params.get("arguments"):
        metadata["arguments"] = arguments
    metadata["mcp_method"] = method

    return AgentEvent(
        tenant_id=tenant_id,
        event_id=span_id,
        timestamp=timestamp,
        platform=Platform.MCP,
        agent_id=agent_id,
        agent_name=client.get("agent_name"),
        actor_id=client.get("principal"),
        actor_type=actor_type,
        event_type=event_type,
        action=action,
        tool_name=tool_name,
        tool_server=record.get("server"),
        resource=record.get("resource"),
        data_domain=classify_domain(record, domains),
        status=status,
        error_code=error_code,
        error_message=error_message,
        duration_ms=record.get("duration_ms"),
        estimated_cost_usd=record.get("cost_usd"),
        model=record.get("model"),
        input_tokens=record.get("input_tokens"),
        output_tokens=record.get("output_tokens"),
        parent_event_id=record.get("parent_span_id"),
        correlation_id=correlation_id,
        session_id=record.get("session_id"),
        source_adapter=ADAPTER_NAME,
        metadata=metadata,
    )


class MCPCallLogSource:
    """`EventSource` over one or more MCP gateway JSONL logs."""

    def __init__(
        self,
        log_path: Path | str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        domains: DataDomainRegistry | None = None,
        strict: bool = False,
    ) -> None:
        self.log_path = Path(log_path)
        self.tenant_id = tenant_id
        self.domains = domains
        self.strict = strict

    @property
    def name(self) -> str:
        return ADAPTER_NAME

    def fetch(self, since: datetime | None = None) -> Iterable[AgentEvent]:
        def mapper(record: dict[str, Any]) -> AgentEvent:
            return to_event(record, self.tenant_id, self.domains)

        for event in normalize_all(self.name, self._read_records(), mapper, strict=self.strict):
            if since is None or event.timestamp >= since:
                yield event

    def _read_records(self) -> Iterator[dict[str, Any]]:
        if not self.log_path.exists():
            raise AdapterError(f"MCP log not found: {self.log_path}")
        with self.log_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                yield json.loads(line)
