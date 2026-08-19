"""Agent, tool and data-domain registries loaded from YAML.

Registries are configuration, not runtime data: they belong in version control
next to the code so a governance change is a reviewable diff. The repository
layer stores *observations*; this stores *expectations*.

Everything here is tenant-scoped at load time. An entry with no explicit
`tenant_id` belongs to the default tenant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from agent_control_plane.constants import DEFAULT_TENANT_ID
from agent_control_plane.models.agent import Agent
from agent_control_plane.models.data_domain import (
    DataDomain,
    DataDomainRegistry,
    DomainMapping,
)
from agent_control_plane.models.enums import Platform
from agent_control_plane.models.tool import Tool


class Registry(BaseModel):
    """Read-only lookup over declared agents, tools and data domains."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str = DEFAULT_TENANT_ID
    agents: dict[str, Agent] = {}
    tools: dict[str, Tool] = {}
    data_domains: DataDomainRegistry = DataDomainRegistry()

    def agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    def tool(self, tool_name: str | None) -> Tool | None:
        if tool_name is None:
            return None
        return self.tools.get(tool_name)

    def is_known_agent(self, agent_id: str) -> bool:
        return agent_id in self.agents

    def classify(self, platform: Platform, resource: str | None) -> str:
        return self.data_domains.classify(platform, resource)


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


def _for_tenant(items: list[Any], tenant_id: str) -> list[Any]:
    return [item for item in items if item.tenant_id == tenant_id]


def load_registry(
    agents_file: Path,
    tools_file: Path,
    domains_file: Path | None = None,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> Registry:
    agents = _for_tenant(
        [Agent.model_validate(i) for i in _load_yaml_list(agents_file, "agents")], tenant_id
    )
    tools = _for_tenant(
        [Tool.model_validate(i) for i in _load_yaml_list(tools_file, "tools")], tenant_id
    )

    if dupes := _duplicates([a.agent_id for a in agents]):
        raise RegistryError(f"duplicate agent_id in {agents_file}: {sorted(dupes)}")
    if dupes := _duplicates([t.tool_name for t in tools]):
        raise RegistryError(f"duplicate tool_name in {tools_file}: {sorted(dupes)}")

    return Registry(
        tenant_id=tenant_id,
        agents={a.agent_id: a for a in agents},
        tools={t.tool_name: t for t in tools},
        data_domains=load_data_domains(domains_file, tenant_id) if domains_file else
        DataDomainRegistry(),
    )


def load_data_domains(
    path: Path | None, tenant_id: str = DEFAULT_TENANT_ID
) -> DataDomainRegistry:
    if path is None or not path.exists():
        return DataDomainRegistry()

    domains = _for_tenant(
        [DataDomain.model_validate(i) for i in _load_yaml_list(path, "domains")], tenant_id
    )
    if dupes := _duplicates([d.name for d in domains]):
        raise RegistryError(f"duplicate data domain in {path}: {sorted(dupes)}")

    mappings = _for_tenant(
        [DomainMapping.model_validate(i) for i in _load_yaml_list(path, "mappings")], tenant_id
    )
    known = {d.name for d in domains}
    # A mapping pointing at an undeclared domain is a config bug that would
    # otherwise surface much later as a mysterious `unknown` classification.
    if orphans := sorted({m.domain for m in mappings} - known):
        raise RegistryError(f"{path}: mappings reference undeclared domains: {orphans}")

    return DataDomainRegistry(
        domains={d.name: d for d in domains}, mappings=tuple(mappings)
    )


def _duplicates(values: list[str]) -> set[str]:
    seen: set[str] = set()
    dupes: set[str] = set()
    for v in values:
        if v in seen:
            dupes.add(v)
        seen.add(v)
    return dupes
