from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_control_plane.bootstrap import ControlPlane, build_control_plane
from agent_control_plane.config import REPO_ROOT, Settings
from agent_control_plane.constants import DEFAULT_TENANT_ID
from agent_control_plane.models.agent import AccessScope, Agent, AgentGovernance
from agent_control_plane.models.data_domain import (
    DataDomain,
    DataDomainRegistry,
    DomainMapping,
)
from agent_control_plane.models.enums import (
    AccessMode,
    ActionType,
    ActorType,
    DataSensitivity,
    EventType,
    Platform,
    Severity,
    Status,
)
from agent_control_plane.models.event import AgentEvent
from agent_control_plane.models.policy import PolicyResult
from agent_control_plane.models.tool import Tool
from agent_control_plane.registry import Registry, load_registry
from agent_control_plane.repositories.sqlite import SQLiteRepository
from agent_control_plane.services.policy_engine import PolicyContext

# Pinned so policy results are byte-identical across runs.
FIXED_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
T0 = datetime(2026, 8, 3, 6, 0, tzinfo=UTC)

# The tenant every fixture belongs to, plus a second one used only to prove
# tenant boundaries hold. Never mix them in a single chain on purpose.
TENANT = DEFAULT_TENANT_ID
OTHER_TENANT = "acme"


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


@pytest.fixture
def vocabulary() -> DataDomainRegistry:
    """A small controlled vocabulary with overlapping mapping rules, so
    most-specific-wins is exercised rather than assumed."""
    domains = [
        DataDomain(name="sales", sensitivity=DataSensitivity.INTERNAL),
        DataDomain(name="finance", sensitivity=DataSensitivity.CONFIDENTIAL),
        DataDomain(name="hr", sensitivity=DataSensitivity.RESTRICTED),
    ]
    mappings = [
        DomainMapping(platform=Platform.SNOWFLAKE, pattern="SALES.*.*", domain="sales"),
        DomainMapping(
            platform=Platform.SNOWFLAKE, pattern="SALES.PUBLIC.*", domain="finance"
        ),
        DomainMapping(platform=Platform.MCP, pattern="ERP://HR/*", domain="hr"),
    ]
    return DataDomainRegistry(
        domains={d.name: d for d in domains}, mappings=tuple(mappings)
    )


@pytest.fixture
def ctx_with_vocabulary(registry: Registry, vocabulary: DataDomainRegistry) -> PolicyContext:
    """Same agents and tools as `ctx`, but with a vocabulary to be unknown against."""
    return PolicyContext(
        registry=registry.model_copy(update={"data_domains": vocabulary}),
        default_max_cost_usd=1.00,
        default_max_tool_calls=25,
        now=FIXED_NOW,
    )


def make_event(**overrides: object) -> AgentEvent:
    """Minimal valid event; override whatever the test is actually about."""
    base: dict[str, object] = {
        "tenant_id": TENANT,
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


def make_result(**overrides: object) -> PolicyResult:
    """Minimal valid policy result. Defaults to a violation, because that is the
    shape with the interesting invariants (evidence, decision)."""
    base: dict[str, object] = {
        "tenant_id": TENANT,
        "result_id": "r1",
        "policy_name": "read_only_violation",
        "severity": Severity.CRITICAL,
        "passed": False,
        "correlation_id": "run-1",
        "event_id": "evt-1",
        "agent_id": "agt_reader",
        "explanation": "tried to write",
        "evidence": {"action": "write"},
        "evaluated_at": FIXED_NOW,
    }
    base.update(overrides)
    return PolicyResult.model_validate(base)
