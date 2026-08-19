"""Persistence interfaces.

Services depend on these Protocols, never on sqlite3/duckdb. Swapping the store
(DuckDB for analytics, Postgres for shared deployments) is then a new class, not
a refactor.

**Every read is tenant-scoped and `tenant_id` is a required argument.** It is
not optional-with-a-default on purpose: a forgettable tenant filter is a tenant
filter someone will forget, and cross-tenant bleed is silent when it happens.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from agent_control_plane.models.event import AgentEvent, ExecutionChain
from agent_control_plane.models.policy import PolicyResult


@runtime_checkable
class EventRepository(Protocol):
    def save_events(self, events: list[AgentEvent]) -> int:
        """Persist events idempotently by (tenant_id, event_id).

        Tenant comes from each event, so a caller cannot accidentally write one
        tenant's data under another's label. Returns rows newly written.
        """
        ...

    def get_event(self, tenant_id: str, event_id: str) -> AgentEvent | None: ...

    def list_events(
        self,
        tenant_id: str,
        *,
        correlation_id: str | None = None,
        agent_id: str | None = None,
        platform: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[AgentEvent]: ...

    def list_correlation_ids(self, tenant_id: str, limit: int | None = None) -> list[str]: ...

    def get_chain(self, tenant_id: str, correlation_id: str) -> ExecutionChain | None: ...

    def count_events(self, tenant_id: str) -> int: ...

    def list_tenants(self) -> list[str]:
        """Every tenant with stored data. The one intentionally unscoped read —
        used by admin tooling to discover what exists."""
        ...


@runtime_checkable
class PolicyResultRepository(Protocol):
    def save_policy_results(self, results: list[PolicyResult]) -> int: ...

    def list_policy_results(
        self,
        tenant_id: str,
        *,
        correlation_id: str | None = None,
        violations_only: bool = False,
        limit: int | None = None,
    ) -> list[PolicyResult]: ...

    def delete_policy_results(self, tenant_id: str, correlation_id: str) -> int:
        """Policy evaluation is re-runnable; results are derived, not source of truth."""
        ...


@runtime_checkable
class ControlPlaneRepository(EventRepository, PolicyResultRepository, Protocol):
    """What the services actually take by injection."""

    def initialise(self) -> None: ...

    def close(self) -> None: ...
