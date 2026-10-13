"""The deterministic demo dataset, defined once for three callers.

`acp seed`, `scripts/seed_demo.py` and the hosted Streamlit demo all build the
same thing from the same committed fixtures. That matters more than it looks:
if the console seeded itself separately, the public demo could drift from what
`acp seed` produces and nobody would notice until the numbers disagreed.

Nothing here is new behaviour — the source assembly moved out of `cli.py`
unchanged, and `ensure_demo_data` is a thin idempotent wrapper over it for the
one caller that cannot run a shell command first.

Credits vs dollars: Snowflake reports credits and the USD rate is contractual,
so the library and the CLI both leave cost unpriced by default rather than
inventing a list price. The demo opts in visibly, via `DEMO_CREDIT_RATE_USD`,
and says so wherever a dollar figure appears.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_control_plane.adapters.base import EventSource
from agent_control_plane.adapters.mcp import MCPCallLogSource
from agent_control_plane.adapters.snowflake import SnowflakeActivitySource
from agent_control_plane.adapters.synthetic import SyntheticSource
from agent_control_plane.bootstrap import ControlPlane, build_control_plane
from agent_control_plane.config import Settings, load_settings
from agent_control_plane.services.ingestion import IngestResult

# Pinned so a reseed produces byte-identical events and the demo's screenshots,
# counts and findings stay stable across restarts.
DEMO_SEED = 20260810

# Snowflake Standard-edition list pricing is the usual back-of-envelope figure.
# A stand-in for a contract rate, not a measurement.
DEMO_CREDIT_RATE_USD = 3.00

MCP_SAMPLE = "mcp_tool_calls.jsonl"
SNOWFLAKE_SAMPLE = "snowflake_activity.jsonl"

# Where a caller that wants to narrate progress (the CLI) receives notes.
Notify = Callable[[str], None]


def _noop(_: str) -> None:
    """Default reporter: the console and the tests have nowhere to print to."""


def _sample(control_plane: ControlPlane, filename: str) -> Path:
    return control_plane.settings.data_dir / "samples" / filename


def demo_sources(
    control_plane: ControlPlane,
    *,
    seed: int = DEMO_SEED,
    mcp_log: Path | str | None = None,
    snowflake_extract: Path | str | None = None,
    credit_rate_usd: float | None = None,
    notify: Notify = _noop,
) -> list[EventSource]:
    """The three demo sources, in ingest order.

    File-backed sources are optional: a missing fixture degrades to a note so
    the demo still runs on synthetic alone, rather than failing on an absent
    file. The synthetic scenario needs nothing from disk and is always present.
    """
    sources: list[EventSource] = [SyntheticSource(seed=seed, tenant_id=control_plane.tenant_id)]

    log_path = Path(mcp_log) if mcp_log else _sample(control_plane, MCP_SAMPLE)
    if log_path.exists():
        sources.append(
            MCPCallLogSource(
                log_path,
                tenant_id=control_plane.tenant_id,
                domains=control_plane.registry.data_domains,
            )
        )
    else:
        notify(f"! no MCP log at {log_path}, skipping that source")

    extract_path = (
        Path(snowflake_extract) if snowflake_extract else _sample(control_plane, SNOWFLAKE_SAMPLE)
    )
    if extract_path.exists():
        sources.append(
            SnowflakeActivitySource(
                extract_path,
                tenant_id=control_plane.tenant_id,
                domains=control_plane.registry.data_domains,
                credit_rate_usd=credit_rate_usd,
            )
        )
        if credit_rate_usd is not None:
            # Say out loud that the USD figures downstream rest on a rate we
            # were handed, not one the telemetry reported.
            notify(
                f"  snowflake credits priced at ${credit_rate_usd:.2f}/credit "
                "(assumption, not telemetry)"
            )
    else:
        notify(f"! no Snowflake extract at {extract_path}, skipping that source")

    return sources


def seed(
    control_plane: ControlPlane,
    *,
    seed_value: int = DEMO_SEED,
    mcp_log: Path | str | None = None,
    snowflake_extract: Path | str | None = None,
    credit_rate_usd: float | None = None,
    notify: Notify = _noop,
) -> list[IngestResult]:
    """Ingest every demo source and evaluate policy over what landed."""
    sources = demo_sources(
        control_plane,
        seed=seed_value,
        mcp_log=mcp_log,
        snowflake_extract=snowflake_extract,
        credit_rate_usd=credit_rate_usd,
        notify=notify,
    )
    return control_plane.ingestion.ingest_all(sources)


# -- public-demo bootstrap ------------------------------------------------
#
# Deliberately separate from `seed` above. `seed` is what an operator asks for;
# this is what the hosted demo does on its own when it finds no data, and it is
# the only part of the system that writes without being told to. Keeping the two
# apart means a future deployment that ingests real telemetry can turn this off
# (`ACP_DEMO_BOOTSTRAP=false`) without losing `acp seed`.


@dataclass(frozen=True, slots=True)
class DemoState:
    """What `ensure_demo_data` found, and whether it had to act."""

    db_path: Path
    tenant_id: str
    event_count: int
    seeded: bool


# Streamlit serves each rerun on a thread from its ScriptRunner pool, so two
# viewers arriving at once can both find an empty database. Writes are
# idempotent by fingerprint, so a race would cost duplicate work rather than
# duplicate rows — but holding the lock makes the first load deterministic and
# keeps the second viewer from watching a half-seeded page.
_SEED_LOCK = threading.Lock()


def ensure_demo_data(settings: Settings | None = None, *, notify: Notify = _noop) -> DemoState:
    """Create the demo dataset if, and only if, the tenant has no events.

    Idempotent by inspection: the check is a tenant-scoped `COUNT(*)`, so a
    populated database is left exactly as it is. Nothing is reset, migrated or
    deleted — `acp seed --reset` remains the only thing that removes data.

    Called on every Streamlit rerun; does real work on at most the first one.
    """
    settings = settings or load_settings()

    with _SEED_LOCK, build_control_plane(settings) as control_plane:
        existing = control_plane.repository.count_events(control_plane.tenant_id)
        if existing:
            return DemoState(
                db_path=settings.db_path,
                tenant_id=control_plane.tenant_id,
                event_count=existing,
                seeded=False,
            )

        notify("seeding the deterministic demo dataset")
        seed(control_plane, credit_rate_usd=DEMO_CREDIT_RATE_USD, notify=notify)
        return DemoState(
            db_path=settings.db_path,
            tenant_id=control_plane.tenant_id,
            event_count=control_plane.repository.count_events(control_plane.tenant_id),
            seeded=True,
        )
