from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agentops.bootstrap import ControlPlane, build_control_plane
from agentops.config import REPO_ROOT, Settings
from agentops.models.agent import AccessScope, Agent, AgentGovernance
from agentops.models.enums import (
    AccessMode,
    ActionType,
    ActorType,
    EventType,
    Platform,
    Status,
)
from agentops.models.event import AgentEvent
from agentops.models.tool import Tool
from agentops.registry import Registry, load_registry
from agentops.repositories.sqlite import SQLiteRepository
from agentops.services.policy_engine import PolicyContext

# Pinned so policy results are byte-identical across runs.
FIXED_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        data_dir=tmp_path,
        registry_dir=REPO_ROOT / "config",
    )


@pytest.fixture
def repo(tmp_path: Path) -> SQLiteRepository:
    repository = SQLiteRepository(tmp_path / "events.db")
    repository.initialise()
    yield repository
    repository.close()


@pytest.fixture
def shipped_registry() -> Registry:
    """The registry actually committed to config/. Exercising it here means a
    typo in governance config fails the build, not production."""
    return load_registry(REPO_ROOT / "config" / "agents.yaml", REPO_ROOT / "config" / "tools.yaml")


@pytest.fixture
def control_plane(settings: Settings) -> ControlPlane:
    with build_control_plane(settings) as cp:
        yield cp


# -- hand-built fixtures for rule unit tests ------------------------------


@pytest.fixture
def registry() -> Registry:
    reader = Agent(
        agent_id="agt_reader",
        name="Reader",
        platform=Platform.LOCAL,
        governance=AgentGovernance(
            access_mode=AccessMode.READ_ONLY,
            tool_access=AccessScope.allowlist("sales.read"),
            data_access=AccessScope.allowlist("sales"),
            max_cost_usd=1.00,
            max_tool_calls=3,
        ),
    )
    writer = Agent(
        agent_id="agt_writer",
        name="Writer",
        platform=Platform.LOCAL,
        governance=AgentGovernance(
            access_mode=AccessMode.READ_WRITE,
            tool_access=AccessScope.allowlist("sales.write"),
            data_access=AccessScope.allowlist("sales"),
            approval_required_actions=frozenset({ActionType.WRITE}),
        ),
    )
    # Explicitly unrestricted, not "empty list therefore unrestricted".
    unlimited = Agent(agent_id="agt_open", name="Open", platform=Platform.LOCAL)
    # Explicitly deny-all: allowlist mode with nothing on it.
    locked = Agent(
        agent_id="agt_locked",
        name="Locked",
        platform=Platform.LOCAL,
        governance=AgentGovernance(
            tool_access=AccessScope.allowlist(),
            data_access=AccessScope.allowlist(),
        ),
    )
    tools = [
        Tool(tool_name="sales.read", data_domains=frozenset({"sales"})),
        Tool(
            tool_name="sales.write",
            is_mutating=True,
            requires_approval=True,
            data_domains=frozenset({"sales"}),
        ),
    ]
    return Registry(
        agents={a.agent_id: a for a in (reader, writer, unlimited, locked)},
        tools={t.tool_name: t for t in tools},
    )


@pytest.fixture
def ctx(registry: Registry) -> PolicyContext:
    return PolicyContext(
        registry=registry,
        default_max_cost_usd=1.00,
        default_max_tool_calls=25,
        now=FIXED_NOW,
    )


def make_event(**overrides: object) -> AgentEvent:
    """Minimal valid event; override whatever the test is actually about."""
    base: dict[str, object] = {
        "event_id": "evt-1",
        "timestamp": T0,
        "platform": Platform.LOCAL,
        "agent_id": "agt_reader",
        "event_type": EventType.TOOL_CALL,
        "action": ActionType.READ,
        "tool_name": "sales.read",
        "data_domain": "sales",
        "status": Status.SUCCESS,
        "correlation_id": "run-1",
        "actor_type": ActorType.AGENT,
    }
    base.update(overrides)
    return AgentEvent.model_validate(base)
