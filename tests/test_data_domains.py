"""The controlled vocabulary and the vendor-object mapping that feeds it.

The invariant under test throughout: a vendor string never becomes a governance
domain by accident. Either a mapping rule says so, or the answer is `unknown`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_control_plane.config import REPO_ROOT
from agent_control_plane.constants import UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.data_domain import (
    DataDomain,
    DataDomainRegistry,
    DomainMapping,
)
from agent_control_plane.models.enums import DataSensitivity, Platform
from agent_control_plane.registry import RegistryError, load_data_domains

SHIPPED = REPO_ROOT / "config" / "data_domains.yaml"


# -- the vocabulary -------------------------------------------------------


@pytest.mark.parametrize("name", ["Sales", "sales-eu", "1sales", "", "sales "])
def test_domain_names_are_a_controlled_shape(name: str) -> None:
    """Loose names are how `Sales`, `sales` and `sales ` become three domains."""
    with pytest.raises(ValidationError):
        DataDomain(name=name)


def test_unknown_is_never_a_known_domain(vocabulary: DataDomainRegistry) -> None:
    """`unknown` is a classification, not something you can be granted."""
    assert vocabulary.is_known("sales")
    assert not vocabulary.is_known(UNKNOWN_DATA_DOMAIN)
    assert not vocabulary.is_known(None)


def test_sensitivity_lookup(vocabulary: DataDomainRegistry) -> None:
    assert vocabulary.sensitivity("hr") is DataSensitivity.RESTRICTED
    assert vocabulary.sensitivity("sales") is DataSensitivity.INTERNAL
    assert vocabulary.sensitivity("nope") is None


def test_unmapped_domains_ignores_the_unknown_sentinel(
    vocabulary: DataDomainRegistry,
) -> None:
    seen = {"sales", "inventry", UNKNOWN_DATA_DOMAIN}
    assert vocabulary.unmapped_domains(seen) == {"inventry"}


# -- classification -------------------------------------------------------


def test_most_specific_mapping_wins(vocabulary: DataDomainRegistry) -> None:
    """`SALES.PUBLIC.*` beats `SALES.*.*` regardless of declaration order."""
    assert vocabulary.classify(Platform.SNOWFLAKE, "SALES.PUBLIC.FCT_ORDERS") == "finance"
    assert vocabulary.classify(Platform.SNOWFLAKE, "SALES.PRIVATE.FCT_ORDERS") == "sales"


def test_matching_is_case_insensitive(vocabulary: DataDomainRegistry) -> None:
    assert vocabulary.classify(Platform.MCP, "erp://hr/headcount") == "hr"


def test_mappings_do_not_leak_across_platforms(vocabulary: DataDomainRegistry) -> None:
    """The same identifier can mean different things on different platforms."""
    assert vocabulary.classify(Platform.MCP, "SALES.PUBLIC.FCT_ORDERS") == UNKNOWN_DATA_DOMAIN


def test_no_match_is_unknown_not_a_guess(vocabulary: DataDomainRegistry) -> None:
    assert vocabulary.classify(Platform.SNOWFLAKE, "RANDOM.DB.TABLE") == UNKNOWN_DATA_DOMAIN
    assert vocabulary.classify(Platform.SNOWFLAKE, None) == UNKNOWN_DATA_DOMAIN


def test_a_mapping_onto_an_undeclared_domain_is_unknown() -> None:
    """Defence in depth: the loader rejects these, but a hand-built registry
    must not smuggle an undeclared name into the core either."""
    registry = DataDomainRegistry(
        domains={"sales": DataDomain(name="sales")},
        mappings=(DomainMapping(platform=Platform.MCP, pattern="ERP://X/*", domain="ghost"),),
    )
    assert registry.classify(Platform.MCP, "erp://x/thing") == UNKNOWN_DATA_DOMAIN


def test_an_empty_registry_classifies_everything_as_unknown() -> None:
    assert DataDomainRegistry().classify(Platform.MCP, "erp://x/y") == UNKNOWN_DATA_DOMAIN


def test_specificity_prefers_fewer_wildcards_then_length() -> None:
    broad = DomainMapping(platform=Platform.SNOWFLAKE, pattern="SALES.*.*", domain="sales")
    narrow = DomainMapping(platform=Platform.SNOWFLAKE, pattern="SALES.PUBLIC.*", domain="sales")
    exact = DomainMapping(
        platform=Platform.SNOWFLAKE, pattern="SALES.PUBLIC.FCT_ORDERS", domain="sales"
    )
    assert exact.specificity > narrow.specificity > broad.specificity


# -- loading --------------------------------------------------------------


def test_the_shipped_vocabulary_loads() -> None:
    """A typo in governance config should fail the build, not production."""
    registry = load_data_domains(SHIPPED)
    assert {"sales", "inventory", "logistics", "hr", "finance"} <= set(registry.domains)
    assert registry.mappings


def test_the_shipped_vocabulary_does_not_declare_unknown() -> None:
    """Declaring it would make it grantable, which defeats the point."""
    assert UNKNOWN_DATA_DOMAIN not in load_data_domains(SHIPPED).domains


def test_the_shipped_mappings_resolve_to_real_domains() -> None:
    registry = load_data_domains(SHIPPED)
    assert all(registry.is_known(m.domain) for m in registry.mappings)


def test_the_shipped_snowflake_mappings_classify_the_scenario_objects() -> None:
    registry = load_data_domains(SHIPPED)
    assert registry.classify(Platform.SNOWFLAKE, "SALES_PROD.PUBLIC.FCT_ORDERS") == "sales"
    assert registry.classify(Platform.MCP, "erp://inventory/stock_levels") == "inventory"
    assert (
        registry.classify(Platform.OTHER, "https://api.weather.gov/gridpoints/forecast")
        == "weather"
    )


def test_a_missing_file_is_an_empty_vocabulary(tmp_path: Path) -> None:
    """No file configured means no vocabulary — not a crash, and not a silent
    default vocabulary nobody reviewed."""
    assert load_data_domains(tmp_path / "nope.yaml").domains == {}
    assert load_data_domains(None).domains == {}


def test_duplicate_domain_rejected(tmp_path: Path) -> None:
    path = tmp_path / "domains.yaml"
    path.write_text("domains:\n  - name: sales\n  - name: sales\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="duplicate data domain"):
        load_data_domains(path)


def test_a_mapping_onto_an_undeclared_domain_is_rejected_at_load(tmp_path: Path) -> None:
    """Otherwise this surfaces much later as a mysterious `unknown`."""
    path = tmp_path / "domains.yaml"
    path.write_text(
        "domains:\n"
        "  - name: sales\n"
        "mappings:\n"
        "  - {platform: mcp, pattern: 'ERP://X/*', domain: ghost}\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="undeclared domains"):
        load_data_domains(path)


def test_domains_and_mappings_are_tenant_scoped_at_load(tmp_path: Path) -> None:
    path = tmp_path / "domains.yaml"
    path.write_text(
        "domains:\n"
        "  - name: sales\n"
        "  - {name: payroll, tenant_id: acme}\n"
        "mappings:\n"
        "  - {platform: mcp, pattern: 'ERP://SALES/*', domain: sales}\n"
        "  - {platform: mcp, pattern: 'ERP://PAY/*', domain: payroll, tenant_id: acme}\n",
        encoding="utf-8",
    )
    default = load_data_domains(path)
    assert set(default.domains) == {"sales"}
    assert default.classify(Platform.MCP, "erp://pay/runs") == UNKNOWN_DATA_DOMAIN

    acme = load_data_domains(path, tenant_id="acme")
    assert set(acme.domains) == {"payroll"}
    assert acme.classify(Platform.MCP, "erp://pay/runs") == "payroll"


def test_a_second_tenant_loads_against_an_empty_vocabulary() -> None:
    """Mappings are tenant-scoped alongside the domains they point at; otherwise
    a tenant with no vocabulary of its own inherits every mapping as an orphan
    and cannot start at all."""
    assert load_data_domains(SHIPPED, tenant_id="acme").domains == {}
    assert load_data_domains(SHIPPED, tenant_id="acme").mappings == ()
