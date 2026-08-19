"""Ingestion: pull from adapters, persist, evaluate policy.

The only place that knows about both adapters and the store. Everything it
touches arrives by injection, so the same service runs against SQLite in prod,
an in-memory repo in tests, and a future DuckDB store without edits.

The service is bound to one tenant and refuses events belonging to any other.
That check is cheap and catches the class of bug — a misconfigured adapter, a
copy-pasted fixture — that would otherwise be invisible until an analytical
result quietly included someone else's data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent_control_plane.adapters.base import EventSource
from agent_control_plane.constants import DEFAULT_TENANT_ID
from agent_control_plane.logging import get_logger, log_context
from agent_control_plane.models.event import AgentEvent
from agent_control_plane.repositories.base import ControlPlaneRepository
from agent_control_plane.services.policy_engine import PolicyContext, PolicyEngine

log = get_logger(__name__)


class TenantMismatchError(RuntimeError):
    """An adapter produced events for a tenant this service does not serve."""


@dataclass(frozen=True, slots=True)
class IngestResult:
    source: str
    tenant_id: str
    fetched: int
    written: int
    correlation_ids: tuple[str, ...] = ()
    policy_results: int = 0
    violations: int = 0

    @property
    def duplicates(self) -> int:
        return self.fetched - self.written


@dataclass(slots=True)
class IngestionService:
    repository: ControlPlaneRepository
    engine: PolicyEngine | None = None
    policy_context: PolicyContext | None = None
    tenant_id: str = DEFAULT_TENANT_ID
    stamp_ingested_at: bool = field(default=True)

    def ingest(
        self,
        source: EventSource,
        *,
        since: datetime | None = None,
        evaluate: bool = True,
    ) -> IngestResult:
        events = list(source.fetch(since))
        self._reject_foreign_tenants(source.name, events)

        if self.stamp_ingested_at:
            now = datetime.now(UTC)
            events = [e.model_copy(update={"ingested_at": now}) for e in events]

        written = self.repository.save_events(events)
        correlation_ids = _ordered_unique(e.correlation_id for e in events)

        log.info(
            "ingested events",
            extra=log_context(
                adapter=source.name,
                tenant=self.tenant_id,
                fetched=len(events),
                written=written,
                runs=len(correlation_ids),
            ),
        )

        results = violations = 0
        if evaluate and correlation_ids:
            results, violations = self.evaluate_runs(correlation_ids)

        return IngestResult(
            source=source.name,
            tenant_id=self.tenant_id,
            fetched=len(events),
            written=written,
            correlation_ids=tuple(correlation_ids),
            policy_results=results,
            violations=violations,
        )

    def _reject_foreign_tenants(self, source_name: str, events: list[AgentEvent]) -> None:
        foreign = {e.tenant_id for e in events} - {self.tenant_id}
        if foreign:
            raise TenantMismatchError(
                f"adapter '{source_name}' produced events for {sorted(foreign)} "
                f"but this control plane serves '{self.tenant_id}'"
            )

    def ingest_all(
        self, sources: Sequence[EventSource], *, since: datetime | None = None
    ) -> list[IngestResult]:
        return [self.ingest(source, since=since) for source in sources]

    def evaluate_runs(self, correlation_ids: Sequence[str]) -> tuple[int, int]:
        """(Re-)evaluate policy for the given runs. Returns (results, violations).

        Policy output is derived data: old results for a run are dropped first so
        a rule change doesn't leave stale findings behind.
        """
        if self.engine is None or self.policy_context is None:
            log.debug("policy evaluation skipped: no engine configured")
            return (0, 0)

        total = violations = 0
        for correlation_id in correlation_ids:
            chain = self.repository.get_chain(self.tenant_id, correlation_id)
            if chain is None:
                continue
            report = self.engine.evaluate(chain, self.policy_context)
            self.repository.delete_policy_results(self.tenant_id, correlation_id)
            self.repository.save_policy_results(list(report.results))
            total += len(report.results)
            violations += len(report.violations)
        return (total, violations)

    def evaluate_everything(self) -> tuple[int, int]:
        """Full re-evaluation. Cheap at MVP scale; revisit when it isn't."""
        return self.evaluate_runs(self.repository.list_correlation_ids(self.tenant_id))

    def ingest_events(
        self, events: list[AgentEvent], *, source_name: str = "direct", evaluate: bool = True
    ) -> IngestResult:
        """Push-style entry point for tests and future webhook/collector ingest."""

        class _Static:
            name = source_name

            def fetch(self, since: datetime | None = None) -> list[AgentEvent]:
                return [e for e in events if since is None or e.timestamp >= since]

        return self.ingest(_Static(), evaluate=evaluate)


def _ordered_unique(values: Iterable[str]) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)
