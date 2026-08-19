"""The central, vendor-neutral event model.

Everything in this system is derived from `AgentEvent`. Adapters produce them,
repositories store them, policies evaluate them, analytics aggregate them. No
vendor SDK types are permitted above the adapter boundary.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_control_plane.models.enums import (
    ActionType,
    ActorType,
    EventType,
    Platform,
    Status,
)


class AgentEvent(BaseModel):
    """A single observed action in an agent execution.

    Chain reconstruction relies on three fields:

    * ``correlation_id`` groups every event in one logical execution (the run).
    * ``parent_event_id`` is the edge in the call tree: Human -> Agent -> Tool -> API.
    * ``actor_id`` / ``actor_type`` record *who* triggered this step, which is how
      an agent-to-agent handoff stays distinguishable from a human request.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Required, not defaulted. A tenant that can be forgotten is a tenant that
    # will be forgotten, and cross-tenant bleed is silent when it happens.
    tenant_id: str = Field(min_length=1, max_length=128)

    event_id: str = Field(min_length=1, max_length=128)
    timestamp: datetime
    platform: Platform

    # --- principal -------------------------------------------------------
    agent_id: str = Field(min_length=1, max_length=128)
    agent_name: str | None = None
    actor_id: str | None = None
    actor_type: ActorType | None = None

    # --- what happened ---------------------------------------------------
    event_type: EventType
    action: ActionType

    tool_name: str | None = None
    tool_server: str | None = Field(
        default=None,
        description="MCP server / gateway that fronted the tool, when applicable.",
    )
    resource: str | None = Field(
        default=None,
        description="Concrete thing touched, e.g. 'SALES.PUBLIC.FCT_ORDERS' or an API URL.",
    )
    data_domain: str | None = Field(
        default=None,
        description="Logical classification used for governance, e.g. 'sales', 'inventory'.",
    )

    status: Status
    error_code: str | None = None
    error_message: str | None = None

    # --- cost / performance ---------------------------------------------
    duration_ms: int | None = Field(default=None, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    # --- chain -----------------------------------------------------------
    parent_event_id: str | None = None
    correlation_id: str = Field(min_length=1, max_length=128)
    session_id: str | None = None

    # --- provenance ------------------------------------------------------
    source_adapter: str = Field(
        default="unknown",
        description="Adapter that produced this event. Kept for lineage and replay.",
    )
    ingested_at: datetime | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timestamp", "ingested_at")
    @classmethod
    def _require_tz(cls, v: datetime | None) -> datetime | None:
        # Naive timestamps silently break cross-platform ordering. Reject early.
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.event_type is EventType.TOOL_CALL and not self.tool_name:
            raise ValueError("tool_call events require tool_name")
        if self.data_domain == "":
            raise ValueError("data_domain must be a domain name or None, never empty")
        if self.parent_event_id == self.event_id:
            raise ValueError("event cannot be its own parent")
        if self.status is Status.FAILURE and not (self.error_code or self.error_message):
            raise ValueError("failure events require error_code or error_message")
        return self

    @property
    def is_root(self) -> bool:
        return self.parent_event_id is None

    def fingerprint(self) -> str:
        """Stable content hash. Lets an adapter mint deterministic ids for
        sources that don't supply one (most audit logs don't)."""
        parts = [
            self.tenant_id,
            self.correlation_id,
            self.agent_id,
            self.event_type,
            self.action,
            self.tool_name or "",
            self.resource or "",
            self.timestamp.isoformat(),
        ]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:32]


class ExecutionChain(BaseModel):
    """All events for one ``correlation_id``, plus the reconstructed call tree.

    A chain belongs to exactly one tenant. Mixing tenants in one chain would let
    a policy rule read another tenant's events as evidence, so it is rejected at
    construction rather than guarded against in every rule.
    """

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    correlation_id: str
    events: tuple[AgentEvent, ...]

    @model_validator(mode="after")
    def _single_tenant(self) -> Self:
        foreign = {e.tenant_id for e in self.events} - {self.tenant_id}
        if foreign:
            raise ValueError(
                f"chain {self.correlation_id} mixes tenants: {sorted(foreign)}"
            )
        return self

    @property
    def roots(self) -> tuple[AgentEvent, ...]:
        known = {e.event_id for e in self.events}
        # Treat an event whose parent is outside this chain as a root, otherwise
        # a truncated export would render as an empty tree.
        return tuple(
            e for e in self.events if e.parent_event_id is None or e.parent_event_id not in known
        )

    def children_of(self, event_id: str) -> tuple[AgentEvent, ...]:
        return tuple(e for e in self.events if e.parent_event_id == event_id)

    @property
    def started_at(self) -> datetime:
        return min(e.timestamp for e in self.events)

    @property
    def ended_at(self) -> datetime:
        return max(e.timestamp for e in self.events)

    @property
    def total_cost_usd(self) -> float:
        return round(sum(e.estimated_cost_usd or 0.0 for e in self.events), 6)

    @property
    def agent_ids(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for e in self.events:
            seen.setdefault(e.agent_id, None)
        return tuple(seen)

    @property
    def failed(self) -> bool:
        return any(e.status in (Status.FAILURE, Status.DENIED, Status.TIMEOUT) for e in self.events)

    def walk(self) -> list[tuple[int, AgentEvent]]:
        """Depth-first (depth, event) pairs, chronological within a level.

        Cycles would be a data bug, but a rendering loop is a worse failure than
        a dropped edge, so visited-tracking is unconditional.
        """
        out: list[tuple[int, AgentEvent]] = []
        visited: set[str] = set()

        def descend(event: AgentEvent, depth: int) -> None:
            if event.event_id in visited:
                return
            visited.add(event.event_id)
            out.append((depth, event))
            for child in sorted(self.children_of(event.event_id), key=lambda e: e.timestamp):
                descend(child, depth + 1)

        for root in sorted(self.roots, key=lambda e: e.timestamp):
            descend(root, 0)

        # Anything orphaned by a cycle still gets emitted, flat.
        for event in sorted(self.events, key=lambda e: e.timestamp):
            if event.event_id not in visited:
                visited.add(event.event_id)
                out.append((0, event))
        return out


def build_chain(
    correlation_id: str, events: list[AgentEvent], tenant_id: str | None = None
) -> ExecutionChain:
    """Assemble a chain. Tenant is taken from the events unless stated, and a
    mixed-tenant batch raises rather than silently picking one."""
    if not events:
        raise ValueError(f"cannot build an empty chain for {correlation_id}")
    ordered = tuple(sorted(events, key=lambda e: (e.timestamp, e.event_id)))
    return ExecutionChain(
        tenant_id=tenant_id or ordered[0].tenant_id,
        correlation_id=correlation_id,
        events=ordered,
    )
