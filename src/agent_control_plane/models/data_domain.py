"""Controlled vocabulary for data domains, plus vendor-object mapping.

The pipeline this exists to enforce:

    vendor object  ->  adapter mapping  ->  controlled governance domain

Free-text classifications must not reach the core. A Snowflake table named
`SALES_PROD.PUBLIC.FCT_ORDERS` becomes the domain `sales` because a mapping
rule says so — not because someone lowercased the database name and hoped.

When nothing matches, the answer is the explicit `unknown` domain and a policy
finding, never the vendor's own string.
"""

from __future__ import annotations

import fnmatch

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_control_plane.constants import DEFAULT_TENANT_ID, UNKNOWN_DATA_DOMAIN
from agent_control_plane.models.enums import DataSensitivity, Platform


class DataDomain(BaseModel):
    """One entry in the controlled vocabulary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    description: str | None = None
    sensitivity: DataSensitivity = DataSensitivity.INTERNAL
    owner: str | None = None
    tenant_id: str = DEFAULT_TENANT_ID


class DomainMapping(BaseModel):
    """Maps a vendor object identifier onto a governance domain.

    ``pattern`` is a case-insensitive glob over the platform's natural
    identifier: ``SALES.*.*`` for a Snowflake ``db.schema.object``,
    ``erp://inventory/*`` for a URI. Globs rather than regex because these are
    written by data owners, not engineers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: Platform
    pattern: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    note: str | None = None
    # Scoped like the domains it points at — a mapping onto another tenant's
    # vocabulary is exactly the orphan the loader refuses to accept.
    tenant_id: str = DEFAULT_TENANT_ID

    @field_validator("pattern")
    @classmethod
    def _normalise(cls, v: str) -> str:
        return v.strip().upper()

    def matches(self, resource: str) -> bool:
        return fnmatch.fnmatchcase(resource.upper(), self.pattern)

    @property
    def specificity(self) -> tuple[int, int]:
        """More literal characters and fewer wildcards wins.

        Sorting by this means `SALES.PUBLIC.FCT_ORDERS` beats `SALES.*`, so
        adding a specific rule never requires reordering the file by hand.
        """
        wildcards = self.pattern.count("*") + self.pattern.count("?")
        return (-wildcards, len(self.pattern))


class DataDomainRegistry(BaseModel):
    """The vocabulary plus its mapping rules."""

    model_config = ConfigDict(frozen=True)

    domains: dict[str, DataDomain] = {}
    mappings: tuple[DomainMapping, ...] = ()

    def is_known(self, domain: str | None) -> bool:
        """`unknown` is a legitimate classification but never a *known* domain —
        that is the whole point of it existing."""
        return domain is not None and domain in self.domains

    def get(self, domain: str | None) -> DataDomain | None:
        return self.domains.get(domain) if domain else None

    def sensitivity(self, domain: str | None) -> DataSensitivity | None:
        entry = self.get(domain)
        return entry.sensitivity if entry else None

    def classify(self, platform: Platform, resource: str | None) -> str:
        """Vendor object -> governance domain. Never returns a vendor string.

        Most specific matching rule wins; no match means `unknown`.
        """
        if not resource:
            return UNKNOWN_DATA_DOMAIN
        candidates = [m for m in self.mappings if m.platform is platform and m.matches(resource)]
        if not candidates:
            return UNKNOWN_DATA_DOMAIN
        best = max(candidates, key=lambda m: m.specificity)
        # A mapping pointing at a domain nobody declared is a config bug; surface
        # it as `unknown` rather than smuggling an undeclared name into the core.
        return best.domain if self.is_known(best.domain) else UNKNOWN_DATA_DOMAIN

    def unmapped_domains(self, domains: set[str]) -> set[str]:
        return {d for d in domains if d != UNKNOWN_DATA_DOMAIN and not self.is_known(d)}
