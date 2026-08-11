"""Policy evaluation results.

A `PolicyResult` is the audit artefact. It must be readable on its own, months
later, without re-running anything — hence evidence + explanation are required
on a failure rather than optional colour.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentops.models.enums import Severity


class PolicyResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    result_id: str = Field(min_length=1, max_length=128)
    policy_name: str = Field(min_length=1, max_length=128)
    severity: Severity
    passed: bool

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

    @model_validator(mode="after")
    def _violations_need_evidence(self) -> Self:
        if not self.passed and not self.evidence:
            raise ValueError("policy violations must carry evidence")
        return self


class PolicyReport(BaseModel):
    """Everything a single run's evaluation produced."""

    model_config = ConfigDict(frozen=True)

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
