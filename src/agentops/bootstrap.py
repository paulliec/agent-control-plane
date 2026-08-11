"""Composition root.

One place where concrete implementations are chosen and wired. The CLI, the
dashboard and the tests all call `build_control_plane`; nothing else constructs
a repository or an engine directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agentops.config import Settings, load_settings
from agentops.models.agent import AgentGovernance
from agentops.registry import Registry, load_registry
from agentops.repositories.base import ControlPlaneRepository
from agentops.repositories.sqlite import SQLiteRepository
from agentops.services.analytics import AnalyticsService
from agentops.services.ingestion import IngestionService
from agentops.services.policy_engine import PolicyContext, PolicyEngine, PolicyRule
from agentops.services.policy_rules import default_rules


@dataclass(frozen=True, slots=True)
class ControlPlane:
    settings: Settings
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
    rules: list[PolicyRule] | None = None,
    repository: ControlPlaneRepository | None = None,
) -> ControlPlane:
    settings = settings or load_settings()
    if db_path is not None:
        settings = settings.model_copy(update={"db_path": Path(db_path)})
    settings.ensure_dirs()

    registry = load_registry(settings.agents_file, settings.tools_file)

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
        registry=registry,
        repository=repo,
        engine=engine,
        policy_context=context,
        ingestion=IngestionService(
            repository=repo, engine=engine, policy_context=context
        ),
        analytics=AnalyticsService(repo),
    )
