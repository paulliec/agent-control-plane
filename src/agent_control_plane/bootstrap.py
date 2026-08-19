"""Composition root.

One place where concrete implementations are chosen and wired. The CLI, the
dashboard and the tests all call `build_control_plane`; nothing else constructs
a repository or an engine directly.

A `ControlPlane` serves exactly one tenant. Serving several means building
several — which is deliberate, because it makes cross-tenant access an explicit
act rather than a forgotten filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.config import Settings, load_settings
from agent_control_plane.models.agent import AgentGovernance
from agent_control_plane.registry import Registry, load_registry
from agent_control_plane.repositories.base import ControlPlaneRepository
from agent_control_plane.repositories.sqlite import SQLiteRepository
from agent_control_plane.services.analytics import AnalyticsService
from agent_control_plane.services.ingestion import IngestionService
from agent_control_plane.services.policy_engine import (
    PolicyContext,
    PolicyEngine,
    PolicyRule,
)
from agent_control_plane.services.policy_rules import default_rules


@dataclass(frozen=True, slots=True)
class ControlPlane:
    settings: Settings
    tenant_id: str
    registry: Registry
    repository: ControlPlaneRepository
    engine: PolicyEngine
    policy_context: PolicyContext
    ingestion: IngestionService
    analytics: AnalyticsService

    def close(self) -> None:
        self.repository.close()

    def __enter__(self) -> ControlPlane:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def build_control_plane(
    settings: Settings | None = None,
    *,
    db_path: Path | str | None = None,
    tenant_id: str | None = None,
    rules: list[PolicyRule] | None = None,
    repository: ControlPlaneRepository | None = None,
) -> ControlPlane:
    settings = settings or load_settings()
    updates: dict[str, object] = {}
    if db_path is not None:
        updates["db_path"] = Path(db_path)
    if tenant_id is not None:
        updates["tenant_id"] = tenant_id
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_dirs()

    registry = load_registry(
        settings.agents_file,
        settings.tools_file,
        settings.data_domains_file,
        tenant_id=settings.tenant_id,
    )

    repo = repository or SQLiteRepository(settings.db_path)
    repo.initialise()

    engine = PolicyEngine(rules or default_rules())
    context = PolicyContext(
        registry=registry,
        default_governance=AgentGovernance(),
        default_max_cost_usd=settings.default_max_cost_usd,
        default_max_tool_calls=settings.default_max_tool_calls,
    )

    return ControlPlane(
        settings=settings,
        tenant_id=settings.tenant_id,
        registry=registry,
        repository=repo,
        engine=engine,
        policy_context=context,
        ingestion=IngestionService(
            repository=repo,
            engine=engine,
            policy_context=context,
            tenant_id=settings.tenant_id,
        ),
        analytics=AnalyticsService(repo, settings.tenant_id),
    )
