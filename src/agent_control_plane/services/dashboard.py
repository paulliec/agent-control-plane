"""Query layer for the console.

Everything the dashboard needs that is *not* a pure aggregate: filtering,
enrichment for display, and the option lists that populate the filter widgets.
It sits on `AnalyticsService` and the repository Protocol — no SQL, no policy
logic, no chain construction of its own.

This exists so the Streamlit layer stays presentation-only. Filtering a finding
by platform means joining a `PolicyResult` to the events of its run, which is
real logic with real edge cases, and logic inside a UI callback is logic nobody
can test. Everything here is deterministic and unit-tested; `dashboard/` just
renders what it returns.

Like `AnalyticsService`, an instance serves exactly one tenant. Cross-tenant
comparison means constructing several, which keeps a tenant filter from being
something a caller can forget.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.enums import Decision, Severity
from agent_control_plane.models.event import AgentEvent, ExecutionChain
from agent_control_plane.models.policy import PolicyResult
from agent_control_plane.repositories.base import ControlPlaneRepository
from agent_control_plane.services.analytics import AnalyticsService, RunSummary

# Rules whose subject is our own missing configuration rather than the agent's
# conduct. The console must not render these as misconduct: nobody can be
# allowlisted for a domain we never classified. Kept as a set rather than a
# severity check because severity is about impact, not about who is at fault.
CONFIGURATION_GAP_RULES = frozenset({"unknown_data_domain", "unregistered_agent"})


@dataclass(frozen=True, slots=True)
class Finding:
    """A `PolicyResult` plus the context needed to filter and explain it.

    The result itself is unchanged and reachable as `.result` — this wraps
    rather than re-models, so nothing here can drift from the audit artefact.
    """

    result: PolicyResult
    platforms: tuple[str, ...]
    sources: tuple[str, ...]
    data_domain: str | None
    resources: tuple[str, ...]
    tool_names: tuple[str, ...]
    occurrences: int

    @property
    def policy_name(self) -> str:
        return self.result.policy_name

    @property
    def severity(self) -> Severity:
        return self.result.severity

    @property
    def would_block(self) -> bool:
        return self.result.would_block

    @property
    def agent_id(self) -> str | None:
        return self.result.agent_id

    @property
    def correlation_id(self) -> str:
        return self.result.correlation_id

    @property
    def is_configuration_gap(self) -> bool:
        """True when the finding is about ACP's own missing knowledge.

        `unknown_data_domain` and `unauthorized_data_domain` look alike in a
        table and mean opposite things: one says the agent exceeded its scope,
        the other says we never wrote a mapping rule. Conflating them blames an
        agent for our backlog.
        """
        return self.policy_name in CONFIGURATION_GAP_RULES

    @property
    def attribution(self) -> str:
        """Who has to act. The word the console renders next to the finding."""
        return "configuration gap" if self.is_configuration_gap else "agent conduct"


@dataclass(frozen=True, slots=True)
class FindingFilter:
    """Empty means unfiltered. A filter naming values that match nothing yields
    nothing — it never silently widens back to everything."""

    platforms: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()
    agents: frozenset[str] = frozenset()
    policies: frozenset[str] = frozenset()
    severities: frozenset[Severity] = frozenset()
    data_domains: frozenset[str] = frozenset()
    would_block_only: bool = False
    correlation_id: str | None = None

    def matches(self, finding: Finding) -> bool:
        if self.platforms and not (self.platforms & set(finding.platforms)):
            return False
        if self.sources and not (self.sources & set(finding.sources)):
            return False
        if self.agents and finding.agent_id not in self.agents:
            return False
        if self.policies and finding.policy_name not in self.policies:
            return False
        if self.severities and finding.severity not in self.severities:
            return False
        if self.data_domains and finding.data_domain not in self.data_domains:
            return False
        if self.would_block_only and not finding.would_block:
            return False
        return not (self.correlation_id and finding.correlation_id != self.correlation_id)


@dataclass(frozen=True, slots=True)
class RunFilter:
    platforms: frozenset[str] = frozenset()
    sources: frozenset[str] = frozenset()
    agents: frozenset[str] = frozenset()
    violations_only: bool = False
    would_block_only: bool = False

    def matches(self, run: RunSummary) -> bool:
        if self.platforms and not (self.platforms & set(run.platforms)):
            return False
        if self.sources and not (self.sources & set(run.sources)):
            return False
        if self.agents and not (self.agents & set(run.agent_ids)):
            return False
        if self.violations_only and not run.violation_count:
            return False
        return not (self.would_block_only and not run.would_block_count)


@dataclass(frozen=True, slots=True)
class DomainActivity:
    """One row of the data-domain view."""

    domain: str
    event_count: int
    agent_count: int
    violation_count: int
    cost_usd: float
    # Costed events, so the UI can say "$x across n of m events" instead of
    # implying the whole domain's spend is known. Snowflake without a credit
    # rate is the case that makes this matter.
    costed_event_count: int

    @property
    def is_classified(self) -> bool:
        return self.domain != UNKNOWN_DATA_DOMAIN


@dataclass(frozen=True, slots=True)
class FilterOptions:
    """Values actually present in this tenant's data.

    Derived from the data rather than from the enums: offering `databricks` in a
    dropdown that can only ever return nothing is a worse experience than a
    short list.
    """

    platforms: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()
    agents: tuple[str, ...] = ()
    policies: tuple[str, ...] = ()
    severities: tuple[Severity, ...] = ()
    data_domains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CostProvenance:
    """How much of the reported spend is measured versus derived.

    Snowflake reports credits; dollars come from multiplying by a rate a caller
    supplied. That is an assumption wearing the same units as a measurement, so
    the console has to be able to say which is which.
    """

    total_usd: float
    costed_events: int
    uncosted_events: int
    sources_with_cost: tuple[str, ...]
    sources_without_cost: tuple[str, ...]
    derived_sources: tuple[str, ...]

    @property
    def has_derived_cost(self) -> bool:
        return bool(self.derived_sources)

    @property
    def coverage(self) -> float:
        total = self.costed_events + self.uncosted_events
        return self.costed_events / total if total else 0.0


# Adapters whose cost is arithmetic over a caller-supplied rate rather than a
# figure the source reported. See Q9 — the model records the number, not where
# the rate came from, so the console names the adapter instead of guessing.
_DERIVED_COST_SOURCES = frozenset({"snowflake"})


@dataclass(slots=True)
class DashboardService:
    """Read-side queries for one tenant's console."""

    repository: ControlPlaneRepository
    analytics: AnalyticsService
    tenant_id: str
    # Cached per instance: the console re-queries on every widget interaction
    # and these are pure reads of an immutable snapshot.
    _events: list[AgentEvent] | None = field(default=None, init=False, repr=False)
    _results: list[PolicyResult] | None = field(default=None, init=False, repr=False)

    # -- primitives -------------------------------------------------------

    def events(self) -> list[AgentEvent]:
        if self._events is None:
            self._events = self.repository.list_events(self.tenant_id)
        return self._events

    def policy_results(self) -> list[PolicyResult]:
        if self._results is None:
            self._results = self.repository.list_policy_results(self.tenant_id)
        return self._results

    def _events_by_run(self) -> dict[str, list[AgentEvent]]:
        grouped: dict[str, list[AgentEvent]] = {}
        for event in self.events():
            grouped.setdefault(event.correlation_id, []).append(event)
        return grouped

    # -- findings ---------------------------------------------------------

    def findings(self, criteria: FindingFilter | None = None) -> list[Finding]:
        """Violations, worst first, enriched and filtered.

        Passing results are excluded: they are how we prove a rule ran, which
        matters in the audit trail and would be noise in a findings table.
        """
        by_run = self._events_by_run()
        criteria = criteria or FindingFilter()
        findings = [
            self._enrich(result, by_run.get(result.correlation_id, []))
            for result in self.policy_results()
            if result.violated
        ]
        selected = [f for f in findings if criteria.matches(f)]
        selected.sort(
            key=lambda f: (-f.severity.rank, not f.would_block, f.correlation_id, f.policy_name)
        )
        return selected

    def _enrich(self, result: PolicyResult, run_events: list[AgentEvent]) -> Finding:
        evidence = result.evidence
        # Prefer the rule's own evidence: a domain rule names the domain it
        # fired on, which is more precise than anything re-derived from events.
        domain = evidence.get("data_domain")
        if domain is None and result.event_id:
            triggering = next((e for e in run_events if e.event_id == result.event_id), None)
            domain = triggering.data_domain if triggering else None

        # A finding is per (agent, subject) within a run, so platform is the
        # platform of that agent's events - not of the whole run, which may
        # legitimately span sources.
        scoped = [e for e in run_events if e.agent_id == result.agent_id] or run_events
        platforms = tuple(sorted({e.platform.value for e in scoped}))
        sources = tuple(sorted({e.source_adapter for e in scoped}))

        return Finding(
            result=result,
            platforms=platforms,
            sources=sources,
            data_domain=str(domain) if domain is not None else None,
            resources=tuple(_string_list(evidence.get("resources"))),
            tool_names=tuple(_string_list(evidence.get("tools") or evidence.get("tool_name"))),
            occurrences=int(evidence.get("occurrences", 1) or 1),
        )

    # -- runs -------------------------------------------------------------

    def runs(self, criteria: RunFilter | None = None, limit: int | None = None) -> list[RunSummary]:
        criteria = criteria or RunFilter()
        summaries = [r for r in self.analytics.recent_runs(limit=10_000) if criteria.matches(r)]
        return summaries[:limit] if limit is not None else summaries

    def chain(self, correlation_id: str) -> ExecutionChain | None:
        return self.analytics.chain(correlation_id)

    def run_findings(self, correlation_id: str) -> list[Finding]:
        return self.findings(FindingFilter(correlation_id=correlation_id))

    # -- data domains -----------------------------------------------------

    def domain_activity(self) -> list[DomainActivity]:
        """Activity, spend and findings per domain, busiest first.

        `unknown` appears here as a first-class row. It is where our
        classification backlog shows up, and hiding it would hide the backlog.
        """
        by_domain: dict[str, list[AgentEvent]] = {}
        for event in self.events():
            if event.data_domain:
                by_domain.setdefault(event.data_domain, []).append(event)

        violations: Counter[str] = Counter()
        for finding in self.findings():
            if finding.data_domain:
                violations[finding.data_domain] += 1

        rows = [
            DomainActivity(
                domain=domain,
                event_count=len(events),
                agent_count=len({e.agent_id for e in events}),
                violation_count=violations.get(domain, 0),
                cost_usd=round(sum(e.estimated_cost_usd or 0.0 for e in events), 6),
                costed_event_count=sum(1 for e in events if e.estimated_cost_usd is not None),
            )
            for domain, events in by_domain.items()
        ]
        rows.sort(key=lambda r: (-r.event_count, r.domain))
        return rows

    def cost_provenance(self) -> CostProvenance:
        events = self.events()
        costed = [e for e in events if e.estimated_cost_usd is not None]
        with_cost = {e.source_adapter for e in costed}
        without_cost = {e.source_adapter for e in events} - with_cost
        return CostProvenance(
            total_usd=round(sum(e.estimated_cost_usd or 0.0 for e in costed), 4),
            costed_events=len(costed),
            uncosted_events=len(events) - len(costed),
            sources_with_cost=tuple(sorted(with_cost)),
            sources_without_cost=tuple(sorted(without_cost)),
            derived_sources=tuple(sorted(with_cost & _DERIVED_COST_SOURCES)),
        )

    # -- filter widgets ---------------------------------------------------

    def filter_options(self) -> FilterOptions:
        events = self.events()
        violations = [r for r in self.policy_results() if r.violated]
        domains = {e.data_domain for e in events if e.data_domain}
        return FilterOptions(
            platforms=tuple(sorted({e.platform.value for e in events})),
            sources=tuple(sorted({e.source_adapter for e in events})),
            agents=tuple(sorted({e.agent_id for e in events})),
            policies=tuple(sorted({r.policy_name for r in violations})),
            severities=tuple(
                sorted({r.severity for r in violations}, key=lambda s: -s.rank),
            ),
            data_domains=tuple(sorted(domains)),
        )

    def decision_breakdown(self) -> dict[str, int]:
        """Violations by what an enforcement point would have done."""
        counts = Counter(r.decision.value for r in self.policy_results() if r.violated)
        return {d.value: counts.get(d.value, 0) for d in Decision if counts.get(d.value)}


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list | tuple):
        return [str(v) for v in value]
    return [str(value)]
