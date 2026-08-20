"""Snowflake activity adapter.

Reads a JSON-lines extract of the shape you get by joining, on ``QUERY_ID``:

* ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`` — who ran what, status, elapsed, credits
* ``SNOWFLAKE.ACCOUNT_USAGE.ACCESS_HISTORY`` — the objects the query touched
* ``SNOWFLAKE.ACCOUNT_USAGE.CORTEX_FUNCTIONS_QUERY_USAGE_HISTORY`` — model, tokens
* ``SNOWFLAKE.ACCOUNT_USAGE.USERS`` — ``USER_TYPE``, to tell a person from a service

Like the MCP adapter it consumes an *extract*, not a live connection: no
credentials, no ``snowflake-connector-python``, testable offline. Pointing this
at a real account means replacing :meth:`SnowflakeActivitySource._read_records`
with a cursor over that join. Nothing above this module changes.

**Two provenances, kept apart.** Snowflake knows what SQL ran, against which
objects, how long it took and what it cost. It does not know what an *agent* is:
``QUERY_HISTORY`` has ``USER_NAME`` and ``ROLE_NAME``, and a service user is not
an agent identity. Agent attribution therefore comes from ``QUERY_TAG``, which
the agent stamps itself — the same epistemic status as an MCP client naming its
own tool, and recorded as such. A row with no parseable tag has no agent, and
we drop it rather than guess one from a username.

**Fan-out.** One query can touch several objects, and each is its own governance
question — a SELECT joining ``SALES`` to ``HR`` is an HR access no matter what
else it did. So one row yields one event *per accessed object*, sharing the
query's correlation id. Collapsing to one event would mean picking a domain and
silently discarding the others.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_control_plane.adapters.base import AdapterError, normalize_all
from agent_control_plane.constants import DEFAULT_TENANT_ID
from agent_control_plane.models.data_domain import DataDomainRegistry
from agent_control_plane.models.enums import (
    ActionType,
    ActorType,
    EventType,
    Platform,
    Status,
)
from agent_control_plane.models.event import AgentEvent

ADAPTER_NAME = "snowflake"

# QUERY_TYPE -> the governance verb. Snowflake has many more; anything absent
# here is dropped rather than guessed, because guessing the verb is guessing
# whether an agent wrote to production.
_QUERY_TYPE_ACTIONS: dict[str, ActionType] = {
    "SELECT": ActionType.READ,
    "DESCRIBE": ActionType.READ,
    "SHOW": ActionType.READ,
    "INSERT": ActionType.WRITE,
    "UPDATE": ActionType.WRITE,
    "MERGE": ActionType.WRITE,
    "COPY": ActionType.WRITE,
    "CREATE_TABLE_AS_SELECT": ActionType.WRITE,
    "DELETE": ActionType.DELETE,
    "TRUNCATE_TABLE": ActionType.DELETE,
    "DROP": ActionType.DELETE,
    "CALL": ActionType.INVOKE,
}

# Snowflake error codes that mean "policy said no", as opposed to "the SQL was
# wrong". Same distinction the MCP adapter draws for gateway rejections: a
# refusal is governance signal, a compilation error is not.
_DENIED_ERROR_CODES = frozenset({"003001", "003011", "090105"})

_USER_TYPE_ACTORS: dict[str, ActorType] = {
    "PERSON": ActorType.HUMAN,
    "SERVICE": ActorType.SERVICE,
    "LEGACY_SERVICE": ActorType.SERVICE,
    "SNOWFLAKE_SERVICE": ActorType.SYSTEM,
}

# Columns the mapper consumes explicitly. Everything else is preserved verbatim
# in `metadata`, so a column we haven't modelled yet is never silently lost.
_KNOWN_COLUMNS = frozenset(
    {
        "QUERY_ID",
        "QUERY_TAG",
        "USER_NAME",
        "USER_TYPE",
        "ROLE_NAME",
        "WAREHOUSE_NAME",
        "QUERY_TYPE",
        "EXECUTION_STATUS",
        "ERROR_CODE",
        "ERROR_MESSAGE",
        "START_TIME",
        "TOTAL_ELAPSED_TIME",
        "CREDITS_USED_CLOUD_SERVICES",
        "BASE_OBJECTS_ACCESSED",
        "OBJECTS_MODIFIED",
        "CORTEX_FUNCTIONS",
    }
)


class SnowflakeTagError(ValueError):
    """QUERY_TAG did not carry usable ACP attribution."""


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        raise ValueError(f"unparseable START_TIME: {value!r}")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_query_tag(raw: Any) -> dict[str, Any]:
    """Pull ACP attribution out of ``QUERY_TAG``.

    The convention is a JSON object with an ``acp`` key::

        {"acp": {"run": "...", "agent": "...", "tool": "...", "step": "..."}}

    Anything else — empty, free text, JSON without ``acp`` — raises. A tag we
    cannot parse is a query we cannot attribute, and a query we cannot attribute
    is not agent activity as far as this control plane is concerned.
    """
    if not raw or not isinstance(raw, str):
        raise SnowflakeTagError("QUERY_TAG is empty; no agent attribution")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SnowflakeTagError(f"QUERY_TAG is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SnowflakeTagError("QUERY_TAG is not a JSON object")
    acp = parsed.get("acp")
    if not isinstance(acp, dict):
        raise SnowflakeTagError("QUERY_TAG has no 'acp' object")
    if not acp.get("agent"):
        raise SnowflakeTagError("QUERY_TAG carries no agent id")
    if not acp.get("run"):
        raise SnowflakeTagError("QUERY_TAG carries no run id")
    return acp


def _status(record: dict[str, Any]) -> tuple[Status, str | None, str | None]:
    execution = str(record.get("EXECUTION_STATUS") or "").upper()
    code = record.get("ERROR_CODE")
    message = record.get("ERROR_MESSAGE")
    if execution == "SUCCESS":
        return Status.SUCCESS, None, None
    if execution in {"RUNNING", "QUEUED", "BLOCKED"}:
        return Status.IN_PROGRESS, None, None
    # FAIL / INCIDENT. AgentEvent insists a failure carries a reason, and
    # ACCOUNT_USAGE always populates one for a failed query.
    code_text = str(code) if code else "SNOWFLAKE_ERROR"
    detail = str(message) if message else code_text
    status = Status.DENIED if code_text in _DENIED_ERROR_CODES else Status.FAILURE
    return status, code_text, detail


def _accessed_objects(record: dict[str, Any]) -> list[tuple[str, bool]]:
    """``(object_name, was_modified)`` for everything the query touched.

    ``OBJECTS_MODIFIED`` is listed after ``BASE_OBJECTS_ACCESSED`` and wins on
    duplicates: if a table was both read and written, the write is the fact that
    matters to governance.
    """
    seen: dict[str, bool] = {}
    for key, modified in (("BASE_OBJECTS_ACCESSED", False), ("OBJECTS_MODIFIED", True)):
        for entry in record.get(key) or []:
            name = entry.get("objectName") if isinstance(entry, dict) else entry
            if name:
                seen[str(name)] = modified
    return list(seen.items())


def _cost_usd(record: dict[str, Any], credit_rate_usd: float | None) -> float | None:
    """Credits become dollars only if someone supplied a rate.

    ``ACCOUNT_USAGE`` reports credits. Converting to USD needs a contract rate
    that is nowhere in the telemetry, so absent a rate this stays ``None`` and
    the raw credits live in ``metadata``. Inventing a list price here would put
    a fabricated number into cost analytics. See Q9.
    """
    if credit_rate_usd is None:
        return None
    credits = float(record.get("CREDITS_USED_CLOUD_SERVICES") or 0.0)
    cortex = record.get("CORTEX_FUNCTIONS") or {}
    credits += float(cortex.get("TOKEN_CREDITS") or 0.0)
    return round(credits * credit_rate_usd, 6)


def _metadata(record: dict[str, Any], *, acp: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {k: v for k, v in record.items() if k not in _KNOWN_COLUMNS}
    extra["query_id"] = record["QUERY_ID"]
    extra["query_type"] = record.get("QUERY_TYPE")
    if role := record.get("ROLE_NAME"):
        extra["role_name"] = role
    if warehouse := record.get("WAREHOUSE_NAME"):
        extra["warehouse_name"] = warehouse
    if credits := record.get("CREDITS_USED_CLOUD_SERVICES"):
        extra["credits_used_cloud_services"] = credits
    if step := acp.get("step"):
        extra["agent_step"] = step
    # The tool name is the agent's own claim, not something Snowflake observed.
    # Recording which fields came from the tag keeps that boundary auditable.
    extra["attributed_by"] = "query_tag"
    return extra


def to_events(
    record: dict[str, Any],
    tenant_id: str = DEFAULT_TENANT_ID,
    domains: DataDomainRegistry | None = None,
    credit_rate_usd: float | None = None,
) -> list[AgentEvent]:
    """Map one joined Snowflake row onto zero or more `AgentEvent`s.

    Zero happens when the row is honest telemetry that ACP has no agent-shaped
    reading of — session housekeeping that touched nothing. Several happens when
    one query touched several objects.
    """
    if not isinstance(record, dict):
        raise ValueError("record must be an object")

    query_id = record.get("QUERY_ID")
    if not query_id:
        raise ValueError("record is missing QUERY_ID")

    acp = parse_query_tag(record.get("QUERY_TAG"))
    agent_id = str(acp["agent"])
    correlation_id = str(acp["run"])

    query_type = str(record.get("QUERY_TYPE") or "").upper()
    if query_type not in _QUERY_TYPE_ACTIONS:
        raise ValueError(f"unsupported QUERY_TYPE: {query_type!r}")
    action = _QUERY_TYPE_ACTIONS[query_type]

    timestamp = _parse_ts(record.get("START_TIME"))
    status, error_code, error_message = _status(record)

    user_type = str(record.get("USER_TYPE") or "").upper()
    actor_type = _USER_TYPE_ACTORS.get(user_type)

    elapsed = record.get("TOTAL_ELAPSED_TIME")
    duration_ms = int(elapsed) if elapsed is not None else None

    cortex = record.get("CORTEX_FUNCTIONS") or {}
    metadata = _metadata(record, acp=acp)

    common: dict[str, Any] = {
        "tenant_id": tenant_id,
        "timestamp": timestamp,
        "platform": Platform.SNOWFLAKE,
        "agent_id": agent_id,
        "actor_id": record.get("USER_NAME"),
        "actor_type": actor_type,
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
        "duration_ms": duration_ms,
        "estimated_cost_usd": _cost_usd(record, credit_rate_usd),
        "correlation_id": correlation_id,
        "source_adapter": ADAPTER_NAME,
    }

    # A Cortex row is an LLM call first and a query second. Reporting it as data
    # access would lose the model, and the objects it touched are incidental.
    if cortex:
        metadata["cortex_function"] = cortex.get("FUNCTION_NAME")
        # ACCOUNT_USAGE reports one TOKENS total. AgentEvent wants input and
        # output separately, and splitting a total is arithmetic fiction — so
        # both stay None and the total is preserved here instead.
        if (tokens := cortex.get("TOKENS")) is not None:
            metadata["cortex_tokens_total"] = tokens
        if (token_credits := cortex.get("TOKEN_CREDITS")) is not None:
            metadata["cortex_token_credits"] = token_credits
        return [
            AgentEvent(
                event_id=str(query_id),
                event_type=EventType.LLM_CALL,
                action=ActionType.EXECUTE,
                model=cortex.get("MODEL_NAME"),
                metadata=metadata,
                **common,
            )
        ]

    objects = _accessed_objects(record)
    if not objects:
        # Tagged, well-formed, and touched nothing a governance rule can speak
        # about. Not an error, just not an event.
        return []

    tool_name = acp.get("tool")
    event_type = EventType.TOOL_CALL if tool_name else EventType.DATA_ACCESS
    registry = domains or DataDomainRegistry()

    events: list[AgentEvent] = []
    for index, (resource, modified) in enumerate(objects):
        events.append(
            AgentEvent(
                # One id per (query, object): stable, and traceable back to the
                # query via metadata["query_id"].
                event_id=str(query_id) if len(objects) == 1 else f"{query_id}#{index}",
                event_type=event_type,
                # A modified object is a write even if QUERY_TYPE read elsewhere.
                action=ActionType.WRITE if modified and action is ActionType.READ else action,
                tool_name=tool_name,
                resource=resource,
                data_domain=registry.classify(Platform.SNOWFLAKE, resource),
                metadata=metadata,
                **common,
            )
        )
    return events


class SnowflakeActivitySource:
    """`EventSource` over a Snowflake activity extract in JSONL."""

    def __init__(
        self,
        extract_path: Path | str,
        *,
        tenant_id: str = DEFAULT_TENANT_ID,
        domains: DataDomainRegistry | None = None,
        credit_rate_usd: float | None = None,
        strict: bool = False,
    ) -> None:
        self.extract_path = Path(extract_path)
        self.tenant_id = tenant_id
        self.domains = domains
        # No default rate on purpose: a made-up list price is worse than no
        # cost number, because it looks like a measurement.
        self.credit_rate_usd = credit_rate_usd
        self.strict = strict

    @property
    def name(self) -> str:
        return ADAPTER_NAME

    def fetch(self, since: datetime | None = None) -> Iterable[AgentEvent]:
        def mapper(record: dict[str, Any]) -> list[AgentEvent]:
            return to_events(record, self.tenant_id, self.domains, self.credit_rate_usd)

        # normalize_all isolates per-record failures; one row may expand to
        # several events or to none, so flatten what it yields.
        for batch in normalize_all(self.name, self._read_records(), mapper, strict=self.strict):
            for event in batch:
                if since is None or event.timestamp >= since:
                    yield event

    def _read_records(self) -> Iterator[dict[str, Any]]:
        if not self.extract_path.exists():
            raise AdapterError(f"Snowflake extract not found: {self.extract_path}")
        with self.extract_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                yield json.loads(line)
