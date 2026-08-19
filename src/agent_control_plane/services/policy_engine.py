"""Policy engine: runs rules over an execution chain and emits audit results.

The engine knows nothing about individual rules. Adding a rule means writing a
class with ``name``, ``severity``, ``description``, ``decision`` and
``evaluate``, then putting it in the list handed to `PolicyEngine`. No
registration decorators, no plugin loader — the rule list *is* the configuration.

**Observe, evaluate, report.** Rules declare what an enforcement point *would*
do (`Decision`), and the engine records it. Nothing here acts on that decision.
See docs/ARCHITECTURE.md for why enforcement is a separate product with
different availability and latency obligations.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from agent_control_plane.logging import get_logger, log_context
from agent_control_plane.models.agent import AgentGovernance
from agent_control_plane.models.enums import Decision, Severity
from agent_control_plane.models.event import ExecutionChain
from agent_control_plane.models.policy import PolicyReport, PolicyResult
from agent_control_plane.registry import Registry

log = get_logger(__name__)

_NAMESPACE = uuid.UUID("6f1f0f16-6c1a-4f2e-8a4a-6d2a2f1a0003")


@dataclass(frozen=True, slots=True)
class Finding:
    """A rule's raw output. The engine decorates it into a `PolicyResult` so
    rules don't each reimplement id generation and timestamping."""

    explanation: str
    evidence: dict[str, Any]
    event_id: str | None = None
    agent_id: str | None = None
    severity: Severity | None = None  # overrides the rule's default when set
    decision: Decision | None = None  # ditto


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything a rule is allowed to consult. Injected, never global."""

    registry: Registry
    default_governance: AgentGovernance = field(default_factory=AgentGovernance)
    default_max_cost_usd: float | None = 1.00
    default_max_tool_calls: int | None = 25
    now: datetime | None = None  # pinned in tests for deterministic results

    def timestamp(self) -> datetime:
        return self.now or datetime.now(UTC)


@runtime_checkable
class PolicyRule(Protocol):
    """Contract for every rule."""

    name: str
    severity: Severity
    description: str
    decision: Decision

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]: ...


class PolicyEngine:
    def __init__(self, rules: Sequence[PolicyRule]) -> None:
        names = [r.name for r in rules]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate policy rule names: {names}")
        self.rules = list(rules)

    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> PolicyReport:
        """Evaluate one run.

        Every rule contributes at least one result: its findings, or a single
        PASS row proving the rule ran. That keeps "no violations" distinguishable
        from "nobody checked" — which matters more than the row count.
        """
        evaluated_at = ctx.timestamp()
        results: list[PolicyResult] = []

        for rule in self.rules:
            try:
                findings = list(rule.evaluate(chain, ctx))
            except Exception:
                # One broken rule must not void the whole evaluation.
                log.exception(
                    "policy rule failed",
                    extra=log_context(rule=rule.name, correlation_id=chain.correlation_id),
                )
                continue

            if not findings:
                results.append(
                    _result(
                        rule=rule,
                        chain=chain,
                        passed=True,
                        explanation=f"{rule.description} No violations detected.",
                        evidence={},
                        event_id=None,
                        agent_id=None,
                        severity=Severity.INFO,
                        decision=Decision.ALLOW,
                        evaluated_at=evaluated_at,
                        discriminator="pass",
                    )
                )
                continue

            for index, finding in enumerate(findings):
                results.append(
                    _result(
                        rule=rule,
                        chain=chain,
                        passed=False,
                        explanation=finding.explanation,
                        evidence=finding.evidence,
                        event_id=finding.event_id,
                        agent_id=finding.agent_id,
                        severity=finding.severity or rule.severity,
                        decision=finding.decision or rule.decision,
                        evaluated_at=evaluated_at,
                        discriminator=finding.event_id or f"{finding.agent_id}:{index}",
                    )
                )

        report = PolicyReport(
            tenant_id=chain.tenant_id,
            correlation_id=chain.correlation_id,
            results=tuple(results),
        )
        if report.violations:
            log.info(
                "policy violations found",
                extra=log_context(
                    tenant=chain.tenant_id,
                    correlation_id=chain.correlation_id,
                    violations=len(report.violations),
                    would_block=len(report.would_block),
                    max_severity=report.max_severity,
                ),
            )
        return report

    def evaluate_all(
        self, chains: Iterable[ExecutionChain], ctx: PolicyContext
    ) -> list[PolicyReport]:
        return [self.evaluate(chain, ctx) for chain in chains]


def _result(
    *,
    rule: PolicyRule,
    chain: ExecutionChain,
    passed: bool,
    explanation: str,
    evidence: dict[str, Any],
    event_id: str | None,
    agent_id: str | None,
    severity: Severity,
    decision: Decision,
    evaluated_at: datetime,
    discriminator: str,
) -> PolicyResult:
    # Deterministic id: re-evaluating a run replaces its results instead of
    # accumulating duplicates.
    key = f"{chain.tenant_id}:{rule.name}:{chain.correlation_id}:{discriminator}"
    result_id = str(uuid.uuid5(_NAMESPACE, key))
    return PolicyResult(
        tenant_id=chain.tenant_id,
        result_id=result_id,
        policy_name=rule.name,
        severity=severity,
        passed=passed,
        decision=decision,
        correlation_id=chain.correlation_id,
        event_id=event_id,
        agent_id=agent_id,
        explanation=explanation,
        evidence=evidence,
        evaluated_at=evaluated_at,
    )


def build_context(
    registry: Registry,
    *,
    default_max_cost_usd: float | None = 1.00,
    default_max_tool_calls: int | None = 25,
    now: datetime | None = None,
) -> PolicyContext:
    return PolicyContext(
        registry=registry,
        default_max_cost_usd=default_max_cost_usd,
        default_max_tool_calls=default_max_tool_calls,
        now=now,
    )
