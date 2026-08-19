"""Policy evaluation results.

A `PolicyResult` is the audit artefact. It must be readable on its own, months
later, without re-running anything — hence evidence + explanation are required
on a failure rather than optional colour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_control_plane.models.enums import Decision, Severity


class PolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=128)
    result_id: str = Field(min_length=1, max_length=128)
    policy_name: str = Field(min_length=1, max_length=128)
    severity: Severity
    passed: bool

    decision: Decision = Field(
        default=Decision.ALLOW,
        description=(
            "What an enforcement point would have done. Recorded, never acted on: "
            "this product observes and reports, it does not sit in the execution path."
        ),
    )
    enforced: bool = Field(
        default=False,
        description=(
            "Whether the decision was actually applied. Always False today. Kept so "
            "an audit trail can distinguish 'we would have blocked this' from "
            "'we blocked this' the day enforcement exists."
        ),
    )

    correlation_id: str
    event_id: str | None = Field(
        default=None,
        description="Event that triggered the finding. None for run-level rules.",
    )
    agent_id: str | None = None

    explanation: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    evaluated_at: datetime

    @property
    def violated(self) -> bool:
        return not self.passed

    @property
    def would_block(self) -> bool:
        """True if enforcement, had it existed, would have stopped the agent."""
        return self.violated and self.decision is not Decision.ALLOW

    @model_validator(mode="after")
    def _violations_need_evidence(self) -> Self:
        if not self.passed and not self.evidence:
            raise ValueError("policy violations must carry evidence")
        if self.passed and self.decision is not Decision.ALLOW:
            raise ValueError("a passing result cannot carry a blocking decision")
        if self.enforced:
            raise ValueError(
                "enforcement is not implemented; a result must not claim to have been enforced"
            )
        return self


class PolicyReport(BaseModel):
    """Everything a single run's evaluation produced."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    correlation_id: str
    results: tuple[PolicyResult, ...]

    @property
    def violations(self) -> tuple[PolicyResult, ...]:
        return tuple(r for r in self.results if r.violated)

    @property
    def max_severity(self) -> Severity | None:
        violations = self.violations
        if not violations:
            return None
        return max((r.severity for r in violations), key=lambda s: s.rank)

    @property
    def would_block(self) -> tuple[PolicyResult, ...]:
        """The subset an enforcement gateway would have stopped. Useful for
        answering 'what would turning this on have cost us?' before turning it on."""
        return tuple(r for r in self.results if r.would_block)
