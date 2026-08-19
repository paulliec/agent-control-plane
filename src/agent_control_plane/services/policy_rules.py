"""The initial rule set.

Rules are deterministic, stateless and independent. Each answers a single
governance question and returns evidence a human can act on without re-reading
the raw log.

Design notes worth remembering:

* **A finding is a problem, not an occurrence.** An agent that retries a
  forbidden tool eight times has one governance problem, not eight. Rules group
  by (agent, subject) within a run and keep every contributing event id in the
  evidence. Without this, a retry storm drowns out a critical finding purely by
  volume.
* A *denied* attempt is still a finding. If a read-only agent reaches for a
  mutating tool and the gateway blocks it, we want that on the record — the
  gateway working is not the same as the agent behaving.
* Scope questions go through `AccessScope.permits()`. Rules never inspect
  whether an allowlist is empty — emptiness carries no meaning, the mode does.
* Rules never mutate state and never call a model. An LLM evaluator, if we ever
  add one, plugs in as another `PolicyRule`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator

from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.agent import AgentGovernance
from agent_control_plane.models.enums import (
    MUTATING_ACTIONS,
    AccessMode,
    ActionType,
    Decision,
    EventType,
    Severity,
    Status,
)
from agent_control_plane.models.event import AgentEvent, ExecutionChain
from agent_control_plane.services.policy_engine import Finding, PolicyContext, PolicyRule

# Evidence keeps every event id, but explanations only name a few.
_MAX_LISTED_EVENTS = 10


def _governance(ctx: PolicyContext, agent_id: str) -> AgentGovernance:
    agent = ctx.registry.agent(agent_id)
    return agent.governance if agent else ctx.default_governance


def _group_by_subject(
    chain: ExecutionChain,
    ctx: PolicyContext,
    detect: Callable[[AgentEvent, PolicyContext], str | None],
) -> Iterator[tuple[str, str, list[AgentEvent]]]:
    """Collapse per-event detections into (agent_id, subject, events) groups.

    ``detect`` returns the subject of the violation (a tool name, a data domain)
    or None when the event is fine. Order is preserved so the first event in
    each group is the earliest occurrence.
    """
    groups: dict[tuple[str, str], list[AgentEvent]] = {}
    for event in chain.events:
        subject = detect(event, ctx)
        if subject is None:
            continue
        groups.setdefault((event.agent_id, subject), []).append(event)
    for (agent_id, subject), events in groups.items():
        yield agent_id, subject, events


def _occurrence_evidence(events: list[AgentEvent]) -> dict[str, object]:
    return {
        "occurrences": len(events),
        "event_ids": [e.event_id for e in events[:_MAX_LISTED_EVENTS]],
        "first_seen": events[0].timestamp.isoformat(),
        "last_seen": events[-1].timestamp.isoformat(),
        "statuses": dict(Counter(e.status.value for e in events)),
    }


def _times(count: int) -> str:
    return "once" if count == 1 else f"{count} times"


# -- scope rules ----------------------------------------------------------


class UnauthorizedDataDomainRule:
    name = "unauthorized_data_domain"
    severity = Severity.HIGH
    description = "Agent touched a data domain outside its declared scope."
    decision = Decision.DENY

    @staticmethod
    def _detect(event: AgentEvent, ctx: PolicyContext) -> str | None:
        # `unknown` means *we* failed to classify the object. Nobody can be
        # allowlisted for it, so flagging the agent here would be a guaranteed
        # HIGH would-block finding for our own gap. `unknown_data_domain` owns it.
        if not event.data_domain or event.data_domain == UNKNOWN_DATA_DOMAIN:
            return None
        gov = _governance(ctx, event.agent_id)
        return None if gov.data_access.permits(event.data_domain) else event.data_domain

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        for agent_id, domain, events in _group_by_subject(chain, ctx, self._detect):
            gov = _governance(ctx, agent_id)
            yield Finding(
                event_id=events[0].event_id,
                agent_id=agent_id,
                explanation=(
                    f"{agent_id} accessed data domain '{domain}' {_times(len(events))}; "
                    f"its data_access scope ({gov.data_access.describe()}) does not permit it."
                ),
                evidence={
                    "data_domain": domain,
                    "data_access_mode": gov.data_access.mode.value,
                    "allowed_data_domains": sorted(gov.data_access.allowed),
                    "resources": sorted({e.resource for e in events if e.resource}),
                    "tools": sorted({e.tool_name for e in events if e.tool_name}),
                    **_occurrence_evidence(events),
                },
            )


class ToolAllowlistRule:
    name = "tool_not_allowlisted"
    severity = Severity.HIGH
    description = "Agent called a tool outside its declared scope."
    decision = Decision.DENY

    @staticmethod
    def _detect(event: AgentEvent, ctx: PolicyContext) -> str | None:
        if event.event_type is not EventType.TOOL_CALL or not event.tool_name:
            return None
        gov = _governance(ctx, event.agent_id)
        return None if gov.tool_access.permits(event.tool_name) else event.tool_name

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        for agent_id, tool_name, events in _group_by_subject(chain, ctx, self._detect):
            gov = _governance(ctx, agent_id)
            yield Finding(
                event_id=events[0].event_id,
                agent_id=agent_id,
                explanation=(
                    f"{agent_id} called '{tool_name}' {_times(len(events))}; its tool_access "
                    f"scope ({gov.tool_access.describe()}) does not permit it."
                ),
                evidence={
                    "tool_name": tool_name,
                    "tool_servers": sorted({e.tool_server for e in events if e.tool_server}),
                    "tool_access_mode": gov.tool_access.mode.value,
                    "allowed_tools": sorted(gov.tool_access.allowed),
                    **_occurrence_evidence(events),
                },
            )


class ReadOnlyViolationRule:
    """Two independent signals of a write: the declared action, and the tool
    registry. Either is enough — an agent's self-reported intent isn't evidence."""

    name = "read_only_violation"
    severity = Severity.CRITICAL
    description = "Read-only agent attempted a state-changing operation."
    decision = Decision.DENY

    @staticmethod
    def _detect(event: AgentEvent, ctx: PolicyContext) -> str | None:
        gov = _governance(ctx, event.agent_id)
        if gov.access_mode is not AccessMode.READ_ONLY:
            return None
        tool = ctx.registry.tool(event.tool_name)
        if event.action in MUTATING_ACTIONS or (tool and tool.is_mutating):
            return event.tool_name or event.resource or event.action.value
        return None

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        for agent_id, subject, events in _group_by_subject(chain, ctx, self._detect):
            first = events[0]
            tool = ctx.registry.tool(first.tool_name)
            by_action = any(e.action in MUTATING_ACTIONS for e in events)
            by_tool = bool(tool and tool.is_mutating)
            reasons = [r for r, hit in (("action", by_action), ("tool_registry", by_tool)) if hit]
            actions = sorted({e.action.value for e in events})
            yield Finding(
                event_id=first.event_id,
                agent_id=agent_id,
                explanation=(
                    f"{agent_id} is configured read_only but attempted "
                    f"{'/'.join(actions)} via '{subject}' {_times(len(events))} "
                    f"(detected by: {', '.join(reasons)})."
                ),
                evidence={
                    "subject": subject,
                    "actions": actions,
                    "tool_name": first.tool_name,
                    "tool_is_mutating": by_tool,
                    "resources": sorted({e.resource for e in events if e.resource}),
                    "detected_by": reasons,
                    "requests": [
                        e.metadata["requested_change"]
                        for e in events
                        if "requested_change" in e.metadata
                    ],
                    **_occurrence_evidence(events),
                },
            )


class ApprovalRequiredRule:
    """Approval must be explicit and linked.

    An approval event counts only if it names what it approves, via metadata
    ``approves_event_id`` or ``approves_tool``. "Someone approved something
    earlier in the run" is not an audit trail.
    """

    name = "missing_human_approval"
    severity = Severity.HIGH
    description = "Action required human approval and none was recorded."
    decision = Decision.REQUIRE_APPROVAL

    @staticmethod
    def _approvals(chain: ExecutionChain) -> tuple[set[str], set[str]]:
        events: set[str] = set()
        tools: set[str] = set()
        for event in chain.events:
            if event.event_type is EventType.APPROVAL and event.action is ActionType.APPROVE:
                if target := event.metadata.get("approves_event_id"):
                    events.add(str(target))
                if tool := event.metadata.get("approves_tool"):
                    tools.add(str(tool))
        return events, tools

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        approved_events, approved_tools = self._approvals(chain)

        def detect(event: AgentEvent, ctx: PolicyContext) -> str | None:
            gov = _governance(ctx, event.agent_id)
            tool = ctx.registry.tool(event.tool_name)
            needs = bool(tool and tool.requires_approval) or (
                event.action in gov.approval_required_actions
            )
            if not needs:
                return None
            if event.event_id in approved_events:
                return None
            if event.tool_name and event.tool_name in approved_tools:
                return None
            return event.tool_name or event.resource or event.action.value

        for agent_id, subject, events in _group_by_subject(chain, ctx, detect):
            gov = _governance(ctx, agent_id)
            first = events[0]
            tool = ctx.registry.tool(first.tool_name)
            yield Finding(
                event_id=first.event_id,
                agent_id=agent_id,
                explanation=(
                    f"{agent_id} performed '{first.action.value}' on '{subject}' "
                    f"{_times(len(events))}; that requires human approval, but no linked "
                    f"approval event was recorded in this run."
                ),
                evidence={
                    "subject": subject,
                    "tool_name": first.tool_name,
                    "actions": sorted({e.action.value for e in events}),
                    "tool_requires_approval": bool(tool and tool.requires_approval),
                    "approval_required_actions": sorted(
                        a.value for a in gov.approval_required_actions
                    ),
                    "approvals_seen": sorted(approved_events | approved_tools),
                    **_occurrence_evidence(events),
                },
            )


class UnknownDataDomainRule:
    """An object nobody classified.

    Two ways to get here, both governance gaps rather than agent misbehaviour:
    the adapter had no mapping rule for the vendor object (`unknown`), or a
    domain name reached the core that isn't in the controlled vocabulary at all
    — which should be impossible via an adapter and means something bypassed it.

    Decision is ALLOW: the agent did nothing wrong, *we* failed to classify the
    data. Blocking on our own config gap would be punishing the wrong party.
    """

    name = "unknown_data_domain"
    severity = Severity.MEDIUM
    description = "Data was accessed under an unclassified governance domain."
    decision = Decision.ALLOW

    @staticmethod
    def _detect(event: AgentEvent, ctx: PolicyContext) -> str | None:
        if not event.data_domain:
            return None
        vocabulary = ctx.registry.data_domains
        if not vocabulary.domains:
            return None  # no vocabulary configured; nothing to be unknown against
        if event.data_domain == UNKNOWN_DATA_DOMAIN:
            return UNKNOWN_DATA_DOMAIN
        return None if vocabulary.is_known(event.data_domain) else event.data_domain

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        for agent_id, domain, events in _group_by_subject(chain, ctx, self._detect):
            unmapped = domain == UNKNOWN_DATA_DOMAIN
            yield Finding(
                event_id=events[0].event_id,
                agent_id=agent_id,
                explanation=(
                    f"{agent_id} accessed data {_times(len(events))} that no mapping "
                    f"rule classifies; add a mapping in config/data_domains.yaml."
                    if unmapped
                    else f"{agent_id} accessed data domain '{domain}' {_times(len(events))}, "
                    f"which is not in the controlled vocabulary."
                ),
                evidence={
                    "data_domain": domain,
                    "reason": "no_mapping_rule" if unmapped else "not_in_vocabulary",
                    "resources": sorted({e.resource for e in events if e.resource}),
                    "platforms": sorted({e.platform.value for e in events}),
                    "tools": sorted({e.tool_name for e in events if e.tool_name}),
                    "known_domains": sorted(ctx.registry.data_domains.domains),
                    **_occurrence_evidence(events),
                },
            )


class UnregisteredAgentRule:
    """Shadow agents. You cannot govern what you have not inventoried."""

    name = "unregistered_agent"
    severity = Severity.MEDIUM
    description = "Activity observed from an agent that is not in the registry."
    decision = Decision.ALLOW

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        for agent_id in chain.agent_ids:
            if ctx.registry.is_known_agent(agent_id):
                continue
            events = [e for e in chain.events if e.agent_id == agent_id]
            yield Finding(
                event_id=events[0].event_id,
                agent_id=agent_id,
                explanation=(
                    f"'{agent_id}' produced {len(events)} event(s) but is not in the "
                    f"agent registry; default governance was applied."
                ),
                evidence={
                    "platforms": sorted({e.platform.value for e in events}),
                    "tools_used": sorted({e.tool_name for e in events if e.tool_name}),
                    "data_domains": sorted({e.data_domain for e in events if e.data_domain}),
                    "actors": sorted({e.actor_id for e in events if e.actor_id}),
                    **_occurrence_evidence(events),
                },
            )


# -- run-level rules ------------------------------------------------------


class CostThresholdRule:
    """Per-agent spend within a run. Attribution beats one run total when two
    agents share a chain."""

    name = "cost_threshold_exceeded"
    severity = Severity.MEDIUM
    description = "Agent spend in a single run exceeded its configured ceiling."
    decision = Decision.DENY

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        totals: dict[str, float] = {}
        for event in chain.events:
            totals[event.agent_id] = totals.get(event.agent_id, 0.0) + (
                event.estimated_cost_usd or 0.0
            )

        for agent_id, spend in totals.items():
            gov = _governance(ctx, agent_id)
            limit = gov.max_cost_usd if gov.max_cost_usd is not None else ctx.default_max_cost_usd
            if limit is None or spend <= limit:
                continue
            top = sorted(
                (e for e in chain.events if e.agent_id == agent_id and e.estimated_cost_usd),
                key=lambda e: e.estimated_cost_usd or 0.0,
                reverse=True,
            )[:3]
            yield Finding(
                event_id=top[0].event_id if top else None,
                agent_id=agent_id,
                explanation=(
                    f"{agent_id} spent ${spend:.4f} in run {chain.correlation_id}, "
                    f"over its ${limit:.2f} ceiling."
                ),
                evidence={
                    "spend_usd": round(spend, 6),
                    "limit_usd": limit,
                    "overage_usd": round(spend - limit, 6),
                    "top_contributors": [
                        {
                            "event_id": e.event_id,
                            "cost_usd": e.estimated_cost_usd,
                            "model": e.model,
                            "tool_name": e.tool_name,
                        }
                        for e in top
                    ],
                },
            )


class ToolCallVolumeRule:
    """Catches retry storms and runaway loops."""

    name = "excessive_tool_calls"
    severity = Severity.MEDIUM
    description = "Agent made an unusually high number of tool calls in one run."
    decision = Decision.DENY

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]:
        calls: Counter[str] = Counter()
        per_tool: dict[str, Counter[str]] = {}
        for event in chain.events:
            if event.event_type is not EventType.TOOL_CALL:
                continue
            calls[event.agent_id] += 1
            per_tool.setdefault(event.agent_id, Counter())[event.tool_name or "?"] += 1

        for agent_id, count in calls.items():
            gov = _governance(ctx, agent_id)
            limit = (
                gov.max_tool_calls if gov.max_tool_calls is not None else ctx.default_max_tool_calls
            )
            if limit is None or count <= limit:
                continue
            failures = sum(
                1
                for e in chain.events
                if e.agent_id == agent_id
                and e.event_type is EventType.TOOL_CALL
                and e.status is not Status.SUCCESS
            )
            yield Finding(
                event_id=None,
                agent_id=agent_id,
                explanation=(
                    f"{agent_id} made {count} tool calls in run {chain.correlation_id} "
                    f"(limit {limit}); {failures} did not succeed."
                ),
                evidence={
                    "tool_call_count": count,
                    "limit": limit,
                    "failed_calls": failures,
                    "by_tool": dict(per_tool.get(agent_id, Counter()).most_common()),
                },
            )


def default_rules() -> list[PolicyRule]:
    """The rule set the CLI and dashboard use unless told otherwise."""
    return [
        ReadOnlyViolationRule(),
        UnauthorizedDataDomainRule(),
        ToolAllowlistRule(),
        ApprovalRequiredRule(),
        CostThresholdRule(),
        ToolCallVolumeRule(),
        UnregisteredAgentRule(),
        UnknownDataDomainRule(),
    ]
