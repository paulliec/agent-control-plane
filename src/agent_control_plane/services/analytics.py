"""Read-side aggregates.

Returns typed results, not DataFrames — the dashboard is one consumer among
several (CLI today, an API later), and none of them should dictate the shape
here. Aggregation happens in Python over the repository Protocol rather than in
SQL, which keeps analytics store-agnostic. At MVP volumes that's free; see
docs/ARCHITECTURE.md for when to push it down.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime

from agent_control_plane.models.enums import EventType, Severity, Status
from agent_control_plane.models.event import AgentEvent, ExecutionChain, build_chain
from agent_control_plane.models.policy import PolicyResult
from agent_control_plane.repositories.base import ControlPlaneRepository

_FAILED_STATUSES = (Status.FAILURE, Status.DENIED, Status.TIMEOUT)


@dataclass(frozen=True, slots=True)
class Overview:
    total_runs: int
    total_events: int
    successful_runs: int
    failed_runs: int
    total_cost_usd: float
    total_tool_calls: int
    failed_tool_calls: int
    violation_count: int
    critical_violations: int
    unique_agents: int
    unique_tools: int
    unregistered_agents: int


@dataclass(frozen=True, slots=True)
class RunSummary:
    correlation_id: str
    started_at: datetime
    ended_at: datetime
    duration_ms: int
    agent_ids: tuple[str, ...]
    initiated_by: str | None
    event_count: int
    tool_call_count: int
    cost_usd: float
    ok: bool
    violation_count: int
    max_severity: Severity | None

    @property
    def status_label(self) -> str:
        return "success" if self.ok else "failed"


@dataclass(frozen=True, slots=True)
class ToolUsage:
    tool_name: str
    call_count: int
    failure_count: int
    cost_usd: float
    agents: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentCost:
    agent_id: str
    agent_name: str | None
    run_count: int
    event_count: int
    cost_usd: float
    violation_count: int


class AnalyticsService:
    """Read-side aggregates for exactly one tenant.

    The tenant is fixed at construction rather than passed per call: every
    aggregate here is a *summary*, and a summary that silently spans tenants is
    the specific failure this design exists to prevent.
    """

    def __init__(self, repository: ControlPlaneRepository, tenant_id: str) -> None:
        self.repository = repository
        self.tenant_id = tenant_id

    # -- primitives -------------------------------------------------------

    def _events(self) -> list[AgentEvent]:
        return self.repository.list_events(self.tenant_id)

    def _violations(self) -> list[PolicyResult]:
        return self.repository.list_policy_results(self.tenant_id, violations_only=True)

    def chains(self) -> list[ExecutionChain]:
        by_run: dict[str, list[AgentEvent]] = {}
        for event in self._events():
            by_run.setdefault(event.correlation_id, []).append(event)
        return [
            build_chain(cid, events, tenant_id=self.tenant_id)
            for cid, events in by_run.items()
        ]

    def chain(self, correlation_id: str) -> ExecutionChain | None:
        return self.repository.get_chain(self.tenant_id, correlation_id)

    # -- aggregates -------------------------------------------------------

    def overview(self) -> Overview:
        events = self._events()
        violations = self._violations()
        chains = _group(events)

        tool_calls = [e for e in events if e.event_type is EventType.TOOL_CALL]
        unregistered = {
            v.agent_id for v in violations if v.policy_name == "unregistered_agent" and v.agent_id
        }

        return Overview(
            total_runs=len(chains),
            total_events=len(events),
            successful_runs=sum(1 for c in chains.values() if not _chain_failed(c)),
            failed_runs=sum(1 for c in chains.values() if _chain_failed(c)),
            total_cost_usd=round(sum(e.estimated_cost_usd or 0.0 for e in events), 4),
            total_tool_calls=len(tool_calls),
            failed_tool_calls=sum(1 for e in tool_calls if e.status in _FAILED_STATUSES),
            violation_count=len(violations),
            critical_violations=sum(1 for v in violations if v.severity is Severity.CRITICAL),
            unique_agents=len({e.agent_id for e in events}),
            unique_tools=len({e.tool_name for e in tool_calls if e.tool_name}),
            unregistered_agents=len(unregistered),
        )

    def events_by_platform(self) -> dict[str, int]:
        counts = Counter(e.platform.value for e in self._events())
        return dict(counts.most_common())

    def events_by_type(self) -> dict[str, int]:
        counts = Counter(e.event_type.value for e in self._events())
        return dict(counts.most_common())

    def top_tools(self, limit: int = 10) -> list[ToolUsage]:
        by_tool: dict[str, list[AgentEvent]] = {}
        for event in self._events():
            if event.event_type is EventType.TOOL_CALL and event.tool_name:
                by_tool.setdefault(event.tool_name, []).append(event)

        usage = [
            ToolUsage(
                tool_name=tool,
                call_count=len(calls),
                failure_count=sum(1 for e in calls if e.status in _FAILED_STATUSES),
                cost_usd=round(sum(e.estimated_cost_usd or 0.0 for e in calls), 6),
                agents=tuple(sorted({e.agent_id for e in calls})),
            )
            for tool, calls in by_tool.items()
        ]
        usage.sort(key=lambda u: (-u.call_count, u.tool_name))
        return usage[:limit]

    def cost_by_agent(self, limit: int = 10) -> list[AgentCost]:
        events = self._events()
        violations = Counter(v.agent_id for v in self._violations() if v.agent_id)

        by_agent: dict[str, list[AgentEvent]] = {}
        for event in events:
            by_agent.setdefault(event.agent_id, []).append(event)

        costs = [
            AgentCost(
                agent_id=agent_id,
                agent_name=next((e.agent_name for e in agent_events if e.agent_name), None),
                run_count=len({e.correlation_id for e in agent_events}),
                event_count=len(agent_events),
                cost_usd=round(sum(e.estimated_cost_usd or 0.0 for e in agent_events), 6),
                violation_count=violations.get(agent_id, 0),
            )
            for agent_id, agent_events in by_agent.items()
        ]
        costs.sort(key=lambda c: (-c.cost_usd, c.agent_id))
        return costs[:limit]

    def cost_by_data_domain(self) -> dict[str, float]:
        # Counter is int-typed; spend is a float. Plain dict, sorted explicitly
        # so ties break on name rather than on iteration order.
        totals: defaultdict[str, float] = defaultdict(float)
        for event in self._events():
            if event.data_domain:
                totals[event.data_domain] += event.estimated_cost_usd or 0.0
        ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
        return {domain: round(value, 6) for domain, value in ranked}

    def recent_runs(self, limit: int = 20) -> list[RunSummary]:
        violations_by_run: dict[str, list[PolicyResult]] = {}
        for result in self._violations():
            violations_by_run.setdefault(result.correlation_id, []).append(result)

        summaries = [
            _summarise(chain, violations_by_run.get(chain.correlation_id, []))
            for chain in self.chains()
        ]
        summaries.sort(key=lambda s: s.started_at, reverse=True)
        return summaries[:limit]

    def run_summary(self, correlation_id: str) -> RunSummary | None:
        chain = self.repository.get_chain(self.tenant_id, correlation_id)
        if chain is None:
            return None
        violations = self.repository.list_policy_results(
            self.tenant_id, correlation_id=correlation_id, violations_only=True
        )
        return _summarise(chain, violations)

    def violations(self, limit: int = 50) -> list[PolicyResult]:
        results = self._violations()
        results.sort(key=lambda r: (-r.severity.rank, r.correlation_id, r.policy_name))
        return results[:limit]

    def violations_by_policy(self) -> dict[str, int]:
        counts = Counter(v.policy_name for v in self._violations())
        return dict(counts.most_common())

    def violations_by_severity(self) -> dict[str, int]:
        counts = Counter(v.severity.value for v in self._violations())
        # Severity order, not count order: a reader scans this for the worst first.
        return {
            s.value: counts.get(s.value, 0)
            for s in sorted(Severity, key=lambda s: -s.rank)
            if counts.get(s.value, 0)
        }


# -- helpers --------------------------------------------------------------


def _group(events: list[AgentEvent]) -> dict[str, list[AgentEvent]]:
    grouped: dict[str, list[AgentEvent]] = {}
    for event in events:
        grouped.setdefault(event.correlation_id, []).append(event)
    return grouped


def _chain_failed(events: list[AgentEvent]) -> bool:
    """A run is 'failed' only if its terminal event failed, or nothing terminal
    exists and something inside failed. Otherwise a run that retried a flaky
    tool and recovered would be scored as a failure."""
    terminal = [e for e in events if e.event_type is EventType.AGENT_RUN_END]
    if terminal:
        last = max(terminal, key=lambda e: e.timestamp)
        return last.status in _FAILED_STATUSES
    return any(e.status in _FAILED_STATUSES for e in events)


def _summarise(chain: ExecutionChain, violations: list[PolicyResult]) -> RunSummary:
    events = list(chain.events)
    roots = chain.roots
    initiated_by = next((r.actor_id for r in roots if r.actor_id), None)
    max_severity = (
        max((v.severity for v in violations), key=lambda s: s.rank) if violations else None
    )
    duration = int((chain.ended_at - chain.started_at).total_seconds() * 1000)

    return RunSummary(
        correlation_id=chain.correlation_id,
        started_at=chain.started_at,
        ended_at=chain.ended_at,
        duration_ms=duration,
        agent_ids=chain.agent_ids,
        initiated_by=initiated_by,
        event_count=len(events),
        tool_call_count=sum(1 for e in events if e.event_type is EventType.TOOL_CALL),
        cost_usd=chain.total_cost_usd,
        ok=not _chain_failed(events),
        violation_count=len(violations),
        max_severity=max_severity,
    )
