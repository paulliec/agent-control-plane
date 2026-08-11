"""Agent and tool registries loaded from YAML.

Registries are configuration, not runtime data: they belong in version control
next to the code so a governance change is a reviewable diff. The repository
layer stores *observations*; this stores *expectations*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from agentops.models.agent import Agent
from agentops.models.tool import Tool


class Registry(BaseModel):
    """Read-only lookup over declared agents and tools."""

    model_config = ConfigDict(frozen=True)

    agents: dict[str, Agent] = {}
    tools: dict[str, Tool] = {}

    def agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def tool(self, tool_name: str | None) -> Tool | None:
        if tool_name is None:
            return None
        return self.tools.get(tool_name)

    def is_known_agent(self, agent_id: str) -> bool:
        return agent_id in self.agents


class RegistryError(RuntimeError):
    pass


def _load_yaml_list(path: Path, key: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise RegistryError(f"{path} must contain a mapping with a '{key}' key")
    items = raw.get(key, [])
    if not isinstance(items, list):
        raise RegistryError(f"{path}:{key} must be a list")
    return items


def load_registry(agents_file: Path, tools_file: Path) -> Registry:
    agents = [Agent.model_validate(item) for item in _load_yaml_list(agents_file, "agents")]
    tools = [Tool.model_validate(item) for item in _load_yaml_list(tools_file, "tools")]

    dupes = _duplicates([a.agent_id for a in agents])
    if dupes:
        raise RegistryError(f"duplicate agent_id in {agents_file}: {sorted(dupes)}")
    dupes = _duplicates([t.tool_name for t in tools])
    if dupes:
        raise RegistryError(f"duplicate tool_name in {tools_file}: {sorted(dupes)}")

    return Registry(
        agents={a.agent_id: a for a in agents},
        tools={t.tool_name: t for t in tools},
    )


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for v in values:
        if v in seen:
            dupes.add(v)
        seen.add(v)
    return dupes
