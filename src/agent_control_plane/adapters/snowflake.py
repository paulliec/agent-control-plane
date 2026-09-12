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
objects, how long it took and what it cost. It also now knows, natively,
*whether an agent invoked the query* — ``QUERY_HISTORY.AGENT_TYPE`` and
``ACCESS_HISTORY.AGENTS_INFO``. What it does not always know is *which logical
agent*: ``CORTEX_AGENT`` can carry a real name, ``CORTEX_LITE_AGENT`` need not
represent a stable named agent, and ``EXTERNAL_AGENT`` may arrive with null id
and name. ``USER_NAME`` stays actor/session identity throughout; a service user
is not an agent.

So three questions are answered separately, and the answers are recorded with
where they came from (``metadata["attribution"]``):

* **Is this agentic, and what type?** Snowflake, natively. Never inferred from
  a username.
* **Which logical agent?** Snowflake when it names one, otherwise the
  ``QUERY_TAG`` the agent stamps itself — a claim, not an observation, and
  labelled as such.
* **Which actor/session?** ``USER_NAME`` / ``USER_TYPE``, as before.

Native identity wins over the tag when both exist, because Snowflake observed
it and the tag is self-declared — but both values are always preserved and
their agreement recorded. A row that is provably agentic yet names no agent
anywhere is dropped rather than pinned on an invented identity; see Q24, that
gap is now the interesting one.

**Fan-out.** One query can touch several objects, and each is its own governance
question — a SELECT joining ``SALES`` to ``HR`` is an HR access no matter what
else it did. So one row yields one event *per accessed object*, sharing the
query's correlation id. Collapsing to one event would mean picking a domain and
silently discarding the others.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
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
        "AGENT_TYPE",
        "AGENTS_INFO",
    }
)

# Native agent types Snowflake reports. Listed so an unfamiliar value is
# preserved verbatim rather than silently normalised into one of these.
NATIVE_AGENT_TYPES = frozenset({"CORTEX_AGENT", "CORTEX_LITE_AGENT", "EXTERNAL_AGENT"})


class SnowflakeAttributionError(ValueError):
    """The row named no agent ACP can honestly use.

    Two distinct situations, and the message says which: the query was not
    agentic at all, or it was provably agentic but nothing — native telemetry
    or tag — identified *which* agent. The second is a real visibility gap and
    is counted as a drop rather than papered over with a fabricated id.
    """


class SnowflakeTagError(SnowflakeAttributionError):
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


def read_query_tag(raw: Any) -> dict[str, Any]:
    """The ``acp`` object if the tag carries one, else empty.

    Lenient counterpart to :func:`parse_query_tag`. Attribution can now come
    from Snowflake itself, so a missing or partial tag is no longer
    automatically fatal — it just contributes nothing.
    """
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    acp = parsed.get("acp")
    return acp if isinstance(acp, dict) else {}


def read_agents_info(raw: Any) -> tuple[dict[str, Any], ...]:
    """``ACCESS_HISTORY.AGENTS_INFO``, order preserved.

    Snowflake orders this nearest-invoking-agent first, top-level agent last.
    That ordering is the meaning of the array, so it is kept exactly as given
    and never sorted or deduplicated.
    """
    if not isinstance(raw, list):
        return ()
    return tuple(entry for entry in raw if isinstance(entry, dict))


def _native_identity(agents_info: tuple[dict[str, Any], ...]) -> tuple[str | None, int | None]:
    """Nearest entry that actually names an agent, and its index.

    Walks outward from the direct invoker rather than reading index 0 blindly:
    a lite agent with no identity may have been invoked by a named Cortex
    agent, and the named one is the honest attribution. The index is recorded
    in provenance so "how far out did we have to look" stays auditable.
    """
    for index, entry in enumerate(agents_info):
        identity = entry.get("agentId") or entry.get("agentName")
        if identity:
            return str(identity), index
    return None, None


@dataclass(frozen=True, slots=True)
class Attribution:
    """Who ACP thinks ran this query, and on whose word.

    Every field pairs a value with its origin. The point is that a reader
    months later can tell an identity Snowflake observed from one an agent
    claimed about itself, without re-reading the extract.
    """

    agent_id: str
    agent_id_source: str
    correlation_id: str
    correlation_id_source: str
    agent_type: str | None
    agent_type_source: str | None
    agents_info: tuple[dict[str, Any], ...]
    native_agent_id: str | None
    native_agent_index: int | None
    tag_agent_id: str | None
    tag: dict[str, Any]

    @property
    def is_native_agentic(self) -> bool:
        """Snowflake itself says an agent invoked this."""
        return self.agent_type is not None

    @property
    def identity_agreement(self) -> str:
        """How the two identity sources relate.

        `differ` is the ordinary case when both exist, not an anomaly: a
        Snowflake agent name and an ACP agent id live in different namespaces
        and would not be expected to match. It records that ACP kept both
        rather than that something is wrong. No rule reads this.
        """
        if self.native_agent_id and self.tag_agent_id:
            return "agree" if self.native_agent_id == self.tag_agent_id else "differ"
        if self.native_agent_id:
            return "only_native"
        return "only_query_tag"


def resolve_attribution(record: dict[str, Any], query_id: str) -> Attribution:
    """Decide agent identity, agent type and run id from one row.

    Precedence for *identity* is native-then-tag, because Snowflake observed
    one and the agent asserted the other. Precedence for *type* is the
    dedicated `AGENT_TYPE` column, then the nearest `AGENTS_INFO` entry.
    Neither is a blanket "native always wins": a native type with no native
    name still leaves the tag as the identity, which is exactly the
    `CORTEX_LITE_AGENT` and `EXTERNAL_AGENT` case.
    """
    agents_info = read_agents_info(record.get("AGENTS_INFO"))
    tag = read_query_tag(record.get("QUERY_TAG"))

    declared = record.get("AGENT_TYPE")
    agent_type = str(declared).upper() if declared else None
    agent_type_source = "query_history.agent_type" if agent_type else None
    if agent_type is None and agents_info and (nearest := agents_info[0].get("agentType")):
        agent_type, agent_type_source = str(nearest).upper(), "snowflake_agents_info"

    native_agent_id, native_index = _native_identity(agents_info)
    tag_agent_id = str(tag["agent"]) if tag.get("agent") else None

    if native_agent_id:
        agent_id, agent_id_source = native_agent_id, "snowflake_agents_info"
    elif tag_agent_id:
        agent_id, agent_id_source = tag_agent_id, "query_tag"
    elif agent_type:
        # Provably agentic, and nobody named the agent. Emitting would mean
        # inventing an identity — USER_NAME is the session, not the agent.
        raise SnowflakeAttributionError(
            f"{agent_type} activity with no resolvable agent identity: "
            "AGENTS_INFO named no agent and QUERY_TAG carries none"
        )
    else:
        raise SnowflakeTagError(
            "no agent attribution: Snowflake reported no agent telemetry "
            "and QUERY_TAG carries no agent id"
        )

    # A tagged run keeps grouping the query into its ACP run. Without one, the
    # query is its own single-event run: Snowflake has no run envelope, which
    # is the same reason its chains are already multi-root.
    if tag.get("run"):
        correlation_id, correlation_id_source = str(tag["run"]), "query_tag"
    else:
        correlation_id, correlation_id_source = query_id, "query_id"

    return Attribution(
        agent_id=agent_id,
        agent_id_source=agent_id_source,
        correlation_id=correlation_id,
        correlation_id_source=correlation_id_source,
        agent_type=agent_type,
        agent_type_source=agent_type_source,
        agents_info=agents_info,
        native_agent_id=native_agent_id,
        native_agent_index=native_index,
        tag_agent_id=tag_agent_id,
        tag=tag,
    )


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


def _metadata(record: dict[str, Any], *, attribution: Attribution) -> dict[str, Any]:
    extra: dict[str, Any] = {k: v for k, v in record.items() if k not in _KNOWN_COLUMNS}
    extra["query_id"] = record["QUERY_ID"]
    extra["query_type"] = record.get("QUERY_TYPE")
    if role := record.get("ROLE_NAME"):
        extra["role_name"] = role
    if warehouse := record.get("WAREHOUSE_NAME"):
        extra["warehouse_name"] = warehouse
    if credits := record.get("CREDITS_USED_CLOUD_SERVICES"):
        extra["credits_used_cloud_services"] = credits
    if step := attribution.tag.get("step"):
        extra["agent_step"] = step

    # Where each answer came from. Small and flat on purpose — this is a
    # record of provenance, not a provenance subsystem. Both identities are
    # kept even when only one was used, so nothing is thrown away.
    extra["attribution"] = {
        "agent_id_source": attribution.agent_id_source,
        "agent_type": attribution.agent_type,
        "agent_type_source": attribution.agent_type_source,
        "actor_id_source": "snowflake_user",
        "correlation_id_source": attribution.correlation_id_source,
        "identity_agreement": attribution.identity_agreement,
        "native_agent_id": attribution.native_agent_id,
        "native_agent_index": attribution.native_agent_index,
        "query_tag_agent_id": attribution.tag_agent_id,
    }
    # The full ordered chain, verbatim. Nearest invoker first. Deliberately not
    # turned into parent_event_id edges: these are attribution context for one
    # query, not separate observed ACP events. See Q24.
    if attribution.agents_info:
        extra["snowflake_agents_info"] = [dict(entry) for entry in attribution.agents_info]

    # Kept as the one-word summary older readers and the docs use. It now names
    # whichever source supplied the identity, not always the tag.
    extra["attributed_by"] = attribution.agent_id_source
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

    attribution = resolve_attribution(record, str(query_id))

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
    metadata = _metadata(record, attribution=attribution)

    common: dict[str, Any] = {
        "tenant_id": tenant_id,
        "timestamp": timestamp,
        "platform": Platform.SNOWFLAKE,
        "agent_id": attribution.agent_id,
        # Actor stays the session/user. Never collapsed into agent identity:
        # a SERVICE_AGENT user is who connected, not which agent reasoned.
        "actor_id": record.get("USER_NAME"),
        "actor_type": actor_type,
        "status": status,
        "error_code": error_code,
        "error_message": error_message,
        "duration_ms": duration_ms,
        "estimated_cost_usd": _cost_usd(record, credit_rate_usd),
        "correlation_id": attribution.correlation_id,
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

    tool_name = attribution.tag.get("tool")
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
