"""Access scope is the one place where a silent default would be a security bug,
so the semantics get their own tests."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent_control_plane.models.agent import AccessScope, Agent, AgentGovernance
from agent_control_plane.models.enums import AccessMode, AccessScopeMode, Platform
from agent_control_plane.registry import load_registry


def _governance(**kwargs: object) -> AgentGovernance:
    return AgentGovernance.model_validate(kwargs)


def test_unrestricted_permits_everything() -> None:
    scope = AccessScope.unrestricted()
    assert scope.permits("anything.at.all")
    assert scope.enforced is False
    assert scope.describe() == "unrestricted"


def test_allowlist_permits_only_what_is_listed() -> None:
    scope = AccessScope.allowlist("a", "b")
    assert scope.permits("a")
    assert not scope.permits("c")
    assert scope.enforced is True


def test_empty_allowlist_denies_everything() -> None:
    """The headline rule: emptiness under allowlist is deny-all, never allow-all."""
    scope = AccessScope.allowlist()
    assert not scope.permits("a")
    assert not scope.permits("")
    assert scope.enforced is True
    assert "deny all" in scope.describe()


def test_unrestricted_with_a_list_is_rejected() -> None:
    """A list under unrestricted reads as a restriction that isn't enforced.
    Refusing it is what stops accidental fail-open configuration."""
    with pytest.raises(ValidationError, match="must not list entries"):
        AccessScope(mode=AccessScopeMode.UNRESTRICTED, allowed=frozenset({"a"}))


def test_mode_is_mandatory() -> None:
    with pytest.raises(ValidationError):
        AccessScope.model_validate({"allowed": ["a"]})


def test_default_governance_is_explicitly_unrestricted_not_empty() -> None:
    gov = AgentGovernance()
    assert gov.tool_access.mode is AccessScopeMode.UNRESTRICTED
    assert gov.data_access.mode is AccessScopeMode.UNRESTRICTED
    assert gov.access_mode is AccessMode.READ_ONLY  # writes still need declaring


# -- the flat YAML spelling -----------------------------------------------


def test_flat_yaml_spelling_is_normalised() -> None:
    gov = _governance(
        tool_access_mode="allowlist",
        allowed_tools=["a", "b"],
        data_access_mode="allowlist",
        allowed_data_domains=["sales"],
    )
    assert gov.tool_access.permits("a")
    assert not gov.tool_access.permits("z")
    assert gov.data_access.allowed == frozenset({"sales"})


def test_flat_allowlist_mode_without_a_list_denies_all() -> None:
    gov = _governance(tool_access_mode="allowlist")
    assert not gov.tool_access.permits("a")


def test_a_list_without_a_mode_is_rejected() -> None:
    """Exactly the ambiguity we removed: a bare list means nothing on its own."""
    with pytest.raises(ValidationError, match="requires an explicit tool_access_mode"):
        _governance(allowed_tools=["a"])


def test_cannot_mix_nested_and_flat_spellings() -> None:
    with pytest.raises(ValidationError, match="not both"):
        _governance(
            tool_access={"mode": "allowlist", "allowed": ["a"]},
            tool_access_mode="allowlist",
            allowed_tools=["b"],
        )


def test_unknown_governance_key_is_rejected() -> None:
    """A typo'd governance key must not silently loosen an agent."""
    with pytest.raises(ValidationError):
        _governance(allowed_toolz=["a"])


def test_agent_registry_roundtrips_through_yaml(tmp_path: Path) -> None:
    agent = Agent(
        agent_id="a1",
        name="A",
        platform=Platform.LOCAL,
        governance=AgentGovernance(
            access_mode=AccessMode.READ_WRITE,
            tool_access=AccessScope.allowlist("t1"),
            data_access=AccessScope.unrestricted(),
        ),
    )
    agents_file = tmp_path / "agents.yaml"
    agents_file.write_text(
        yaml.safe_dump({"agents": [agent.model_dump(mode="json")]}), encoding="utf-8"
    )

    loaded = load_registry(agents_file, tmp_path / "tools.yaml").agents["a1"]
    assert loaded == agent
    assert loaded.governance.tool_access.permits("t1")
    assert loaded.governance.data_access.permits("anything")
