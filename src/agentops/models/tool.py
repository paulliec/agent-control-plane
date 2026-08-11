"""Tool inventory.

The tool registry exists so governance doesn't depend on an agent honestly
reporting its own intent. An event may claim ``action=invoke``, but if the
registry says ``inventory.adjust_stock`` mutates state, the read-only rule
fires anyway.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agentops.models.enums import Platform


class Tool(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1, max_length=200)
    display_name: str | None = None
    platform: Platform = Platform.MCP
    server: str | None = Field(
        default=None, description="MCP server or gateway exposing this tool."
    )
    description: str | None = None

    is_mutating: bool = Field(
        default=False,
        description="True if the tool can change state anywhere downstream.",
    )
    requires_approval: bool = False
    data_domains: frozenset[str] = frozenset()
    downstream_systems: frozenset[str] = frozenset()
    tags: frozenset[str] = frozenset()
