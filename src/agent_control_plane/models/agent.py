"""Agent inventory and the governance envelope attached to each agent.

Governance lives with the agent record on purpose: it is the same shape as an
IAM role attached to a principal. Policy *rules* stay generic; the per-agent
limits they read live here.

Access scope is always **two fields**: a mode and a list. An empty list carries
no security meaning of its own — see `AccessScope`.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent_control_plane.constants import DEFAULT_TENANT_ID
from agent_control_plane.models.enums import AccessMode, AccessScopeMode, ActionType, Platform


class AccessScope(BaseModel):
    """An explicit allow decision over a set of names (tools, data domains).

    The mode is mandatory and the empty set is *not* overloaded:

    * ``unrestricted``            -> everything is permitted, and ``allowed`` must be empty
    * ``allowlist`` + ``{a, b}``  -> only a and b
    * ``allowlist`` + ``{}``      -> nothing is permitted (deny-all)

    Reading permission out of "is this collection empty?" is how you end up
    fail-open by accident: someone deletes the last entry from a list and the
    agent silently gains the world. Requiring the mode makes both the intent and
    the diff explicit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: AccessScopeMode
    allowed: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def _mode_and_list_must_agree(self) -> Self:
        # An unrestricted scope carrying a list reads as a restriction that
        # isn't enforced. Refuse it rather than quietly ignoring the list.
        if self.mode is AccessScopeMode.UNRESTRICTED and self.allowed:
            raise ValueError(
                "mode=unrestricted must not list entries; "
                "use mode=allowlist if the list is meant to restrict anything"
            )
        return self

    def permits(self, name: str) -> bool:
        if self.mode is AccessScopeMode.UNRESTRICTED:
            return True
        return name in self.allowed

    @property
    def enforced(self) -> bool:
        return self.mode is AccessScopeMode.ALLOWLIST

    def describe(self) -> str:
        if self.mode is AccessScopeMode.UNRESTRICTED:
            return "unrestricted"
        if not self.allowed:
            return "allowlist (empty: deny all)"
        return f"allowlist ({len(self.allowed)})"

    @classmethod
    def unrestricted(cls) -> AccessScope:
        return cls(mode=AccessScopeMode.UNRESTRICTED)

    @classmethod
    def allowlist(cls, *names: str) -> AccessScope:
        return cls(mode=AccessScopeMode.ALLOWLIST, allowed=frozenset(names))


class AgentGovernance(BaseModel):
    """Declared expectations for an agent.

    Defaults are deliberately permissive-but-explicit: an agent registered
    without a scope is `unrestricted`, which is visible in the config and in the
    dashboard, rather than a silently empty allowlist that means nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    access_mode: AccessMode = AccessMode.READ_ONLY
    tool_access: AccessScope = AccessScope(mode=AccessScopeMode.UNRESTRICTED)
    data_access: AccessScope = AccessScope(mode=AccessScopeMode.UNRESTRICTED)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)
    approval_required_actions: frozenset[ActionType] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _accept_flat_yaml(cls, data: object) -> object:
        """Accept the flatter YAML spelling and normalise it.

        ``tool_access_mode: allowlist`` + ``allowed_tools: [...]`` is nicer to
        read and diff in config than a nested object, but the model stays a
        single `AccessScope` so rules only ever ask ``permits()``.
        """
        if not isinstance(data, dict):
            return data
        out = dict(data)
        for prefix, field_name, list_key in (
            ("tool_access_mode", "tool_access", "allowed_tools"),
            ("data_access_mode", "data_access", "allowed_data_domains"),
        ):
            mode = out.pop(prefix, None)
            allowed = out.pop(list_key, None)
            if mode is None and allowed is None:
                continue
            if field_name in out:
                raise ValueError(
                    f"specify either {field_name} or {prefix}/{list_key}, not both"
                )
            if mode is None:
                # A list without a mode is exactly the ambiguity we are removing.
                raise ValueError(f"{list_key} requires an explicit {prefix}")
            out[field_name] = {"mode": mode, "allowed": allowed or []}
        return out


class Agent(BaseModel):
    """A registered agent. Events referencing an unregistered agent_id are
    themselves a governance finding (shadow agents)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = DEFAULT_TENANT_ID
    agent_id: str = Field(min_length=1, max_length=128)
    name: str
    platform: Platform
    owner: str | None = None
    description: str | None = None
    tags: frozenset[str] = frozenset()
    governance: AgentGovernance = AgentGovernance()
