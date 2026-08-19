"""Vendor-neutral vocabularies shared by the domain models.

These are deliberately small and closed. Adapters own the job of mapping a
vendor's vocabulary onto these values; if a mapping doesn't exist yet, that is a
signal to extend the enum consciously rather than to let vendor strings leak
into the core.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    """Where the execution physically happened."""

    LOCAL = "local"
    MCP = "mcp"
    SNOWFLAKE = "snowflake"
    DATABRICKS = "databricks"
    FOUNDRY = "foundry"
    OTHER = "other"


class ActorType(StrEnum):
    """What kind of principal initiated an event.

    Needed to distinguish a human-initiated run from agent-to-agent delegation,
    which is the difference between an audit trail and a guess.
    """

    HUMAN = "human"
    AGENT = "agent"
    SERVICE = "service"
    SYSTEM = "system"


class EventType(StrEnum):
    """Category of event. Determines which fields are meaningful."""

    AGENT_RUN_START = "agent_run_start"
    AGENT_RUN_END = "agent_run_end"
    AGENT_STEP = "agent_step"
    AGENT_HANDOFF = "agent_handoff"
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    DATA_ACCESS = "data_access"
    APPROVAL = "approval"


class ActionType(StrEnum):
    """The verb. Drives read-only / write governance independently of tooling."""

    START = "start"
    FINISH = "finish"
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"
    INVOKE = "invoke"
    DELEGATE = "delegate"
    APPROVE = "approve"
    REJECT = "reject"


class Status(StrEnum):
    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"
    TIMEOUT = "timeout"
    IN_PROGRESS = "in_progress"


class AccessMode(StrEnum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class AccessScopeMode(StrEnum):
    """How to read an allow-list.

    Explicit on purpose: an empty collection must never be the thing that
    decides whether an agent is restricted. See `models.agent.AccessScope`.
    """

    UNRESTRICTED = "unrestricted"
    ALLOWLIST = "allowlist"


class Decision(StrEnum):
    """What an enforcement point *would* do about a finding.

    The product is observe-only: nothing in this codebase acts on a Decision.
    It exists so rules can express intent now, and so a future enforcement
    gateway can consume the same rules without them being rewritten.

    Observation and enforcement have genuinely different obligations — see
    docs/ARCHITECTURE.md — which is exactly why the decision is recorded
    separately from the act of enforcing it.
    """

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class DataSensitivity(StrEnum):
    """How badly it hurts if the wrong agent reads this domain."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class Severity(StrEnum):
    """Ordered low -> critical. Use `Severity.rank` for comparisons."""

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_RANK[self]


_SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

# Actions that mutate state. Kept here so the read-only rule and the tool
# registry agree on what "write" means.
MUTATING_ACTIONS: frozenset[ActionType] = frozenset(
    {ActionType.WRITE, ActionType.DELETE}
)
