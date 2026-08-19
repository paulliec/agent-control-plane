"""Shared literals that would otherwise be typo-prone magic strings.

Deliberately dependency-free so both `models/` and `config/` can import it.
"""

from __future__ import annotations

# Single-tenant deployments still carry a tenant. The point is that the schema
# never assumes only one organisation can exist — not that we ship SaaS tenancy.
DEFAULT_TENANT_ID = "default"

# The classification an adapter emits when it cannot map a vendor object onto
# the controlled vocabulary. It is a real, visible value that a policy rule
# reacts to — never None, and never the vendor's own string.
UNKNOWN_DATA_DOMAIN = "unknown"
