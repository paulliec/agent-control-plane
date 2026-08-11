"""Persistence interfaces.

Services depend on these Protocols, never on sqlite3/duckdb. Swapping the store
(DuckDB for analytics, Postgres for shared deployments) is then a new class, not
a refactor.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from agentops.models.event import AgentEvent, ExecutionChain
from agentops.models.policy import PolicyResult


class EventFilter(Protocol):
    """Marker for the filter object accepted by queries (see `query.EventQuery`)."""


@runtime_checkable
class EventRepository(Protocol):
    def save_events(self, events: list[AgentEvent]) -> int:
        """Persist events idempotently by ``event_id``. Returns rows newly written."""
        ...

    def get_event(self, event_id: str) -> AgentEvent | None: ...

    def list_events(
        self,
        *,
        correlation_id: str | None = None,
        agent_id: str | None = None,
        platform: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[AgentEvent]: ...

    def list_correlation_ids(self, limit: int | None = None) -> list[str]: ...

    def get_chain(self, correlation_id: str) -> ExecutionChain | None: ...

    def count_events(self) -> int: ...


@runtime_checkable
class PolicyResultRepository(Protocol):
    def save_policy_results(self, results: list[PolicyResult]) -> int: ...

    def list_policy_results(
        self,
        *,
        correlation_id: str | None = None,
        violations_only: bool = False,
        limit: int | None = None,
    ) -> list[PolicyResult]: ...

    def delete_policy_results(self, correlation_id: str) -> int:
        """Policy evaluation is re-runnable; results are derived, not source of truth."""
        ...


@runtime_checkable
class ControlPlaneRepository(EventRepository, PolicyResultRepository, Protocol):
    """What the services actually take by injection."""

    def initialise(self) -> None: ...

    def close(self) -> None: ...
