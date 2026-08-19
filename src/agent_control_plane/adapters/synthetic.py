"""Synthetic Weekly Sales Forecast scenario.

Stands in for real platform telemetry until the Snowflake/Databricks/Foundry
adapters land. Deterministic for a given seed so tests and demos are stable.

The narrative, per run:

    Human analyst
      └─ Weekly Sales Forecast Agent
           ├─ sales.query_history        -> Snowflake
           ├─ promotions.get_calendar    -> Snowflake
           ├─ weather.get_forecast (MCP) -> api.weather.gov
           ├─ forecast.run_model         -> local
           ├─ anomaly detected
           │    └─ handoff: Inventory Investigator Agent
           │         └─ inventory.read_stock_levels (MCP) -> ERP
           └─ reporting.render_draft

Four variants are produced so the dashboard has something to say:
``clean``, ``degraded`` (upstream timeout + retries), ``violation`` (read-only
agent reaches for a mutating tool and an unauthorised domain), and ``shadow``
(an agent nobody registered).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from random import Random
from typing import Any, Literal

from agent_control_plane.constants import DEFAULT_TENANT_ID
from agent_control_plane.models.enums import (
    ActionType,
    ActorType,
    EventType,
    Platform,
    Status,
)
from agent_control_plane.models.event import AgentEvent

ADAPTER_NAME = "synthetic"

# Fixed namespace -> ids are reproducible across machines and runs.
_NAMESPACE = uuid.UUID("6f1f0f16-6c1a-4f2e-8a4a-6d2a2f1a0001")

FORECAST_AGENT = ("agt_forecast_weekly", "Weekly Sales Forecast Agent")
INVENTORY_AGENT = ("agt_inventory_investigator", "Inventory Investigator Agent")
PUBLISHER_AGENT = ("agt_report_publisher", "Report Publisher Agent")
SHADOW_AGENT = ("agt_pricing_experiment", "Pricing Experiment Agent")

ANALYST = "paul.carson@contoso.com"
APPROVER = "maria.gomez@contoso.com"

Variant = Literal["clean", "degraded", "violation", "shadow", "governed_publish"]


@dataclass(slots=True)
class _RunBuilder:
    """Accumulates one run's events, advancing a clock as it goes."""

    tenant_id: str
    correlation_id: str
    clock: datetime
    rng: Random
    session_id: str
    events: list[AgentEvent] = field(default_factory=list)
    _seq: int = 0

    def tick(self, ms: int) -> None:
        self.clock += timedelta(milliseconds=ms)

    def emit(
        self,
        *,
        agent: tuple[str, str],
        event_type: EventType,
        action: ActionType,
        status: Status = Status.SUCCESS,
        platform: Platform = Platform.LOCAL,
        parent: AgentEvent | None = None,
        actor_id: str | None = None,
        actor_type: ActorType = ActorType.AGENT,
        tool_name: str | None = None,
        tool_server: str | None = None,
        resource: str | None = None,
        data_domain: str | None = None,
        duration_ms: int | None = None,
        cost_usd: float | None = None,
        model: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentEvent:
        self._seq += 1
        event_id = str(uuid.uuid5(_NAMESPACE, f"{self.correlation_id}:{self._seq}"))
        agent_id, agent_name = agent
        event = AgentEvent(
            tenant_id=self.tenant_id,
            event_id=event_id,
            timestamp=self.clock,
            platform=platform,
            agent_id=agent_id,
            agent_name=agent_name,
            actor_id=actor_id if actor_id is not None else (parent.agent_id if parent else None),
            actor_type=actor_type,
            event_type=event_type,
            action=action,
            tool_name=tool_name,
            tool_server=tool_server,
            resource=resource,
            data_domain=data_domain,
            status=status,
            error_code=error_code,
            error_message=error_message,
            duration_ms=duration_ms,
            estimated_cost_usd=cost_usd,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            parent_event_id=parent.event_id if parent else None,
            correlation_id=self.correlation_id,
            session_id=self.session_id,
            source_adapter=ADAPTER_NAME,
            metadata=metadata or {},
        )
        self.events.append(event)
        if duration_ms:
            self.tick(duration_ms)
        else:
            self.tick(self.rng.randint(50, 400))
        return event


def _jitter(rng: Random, base: int, spread: float = 0.35) -> int:
    return max(1, int(base * (1 + rng.uniform(-spread, spread))))


def _cost(rng: Random, base: float) -> float:
    return round(base * (1 + rng.uniform(-0.2, 0.25)), 6)


# -- scenario steps -------------------------------------------------------


def _start_run(b: _RunBuilder, week_of: str) -> AgentEvent:
    return b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.AGENT_RUN_START,
        action=ActionType.START,
        actor_id=ANALYST,
        actor_type=ActorType.HUMAN,
        duration_ms=_jitter(b.rng, 40),
        metadata={"week_of": week_of, "trigger": "schedule:weekly-monday-0600"},
    )


def _query_sales(b: _RunBuilder, root: AgentEvent) -> AgentEvent:
    call = b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.READ,
        platform=Platform.MCP,
        parent=root,
        tool_name="sales.query_history",
        tool_server="snowflake-mcp",
        data_domain="sales",
        duration_ms=_jitter(b.rng, 1800),
        cost_usd=_cost(b.rng, 0.012),
        metadata={"lookback_weeks": 52},
    )
    b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.DATA_ACCESS,
        action=ActionType.READ,
        platform=Platform.SNOWFLAKE,
        parent=call,
        resource="SALES.PUBLIC.FCT_ORDERS",
        data_domain="sales",
        duration_ms=_jitter(b.rng, 1200),
        cost_usd=_cost(b.rng, 0.031),
        metadata={"warehouse": "WH_ANALYTICS_XS", "rows_scanned": b.rng.randint(80_000, 250_000)},
    )
    return call


def _query_promotions(b: _RunBuilder, root: AgentEvent) -> AgentEvent:
    call = b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.READ,
        platform=Platform.MCP,
        parent=root,
        tool_name="promotions.get_calendar",
        tool_server="snowflake-mcp",
        data_domain="promotions",
        duration_ms=_jitter(b.rng, 700),
        cost_usd=_cost(b.rng, 0.004),
    )
    b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.DATA_ACCESS,
        action=ActionType.READ,
        platform=Platform.SNOWFLAKE,
        parent=call,
        resource="MARKETING.PUBLIC.DIM_PROMOTION",
        data_domain="promotions",
        duration_ms=_jitter(b.rng, 500),
        cost_usd=_cost(b.rng, 0.006),
    )
    return call


def _weather(b: _RunBuilder, root: AgentEvent, *, degraded: bool = False) -> AgentEvent:
    """Human -> Agent -> MCP tool -> external API, the canonical chain."""
    if degraded:
        failed = b.emit(
            agent=FORECAST_AGENT,
            event_type=EventType.TOOL_CALL,
            action=ActionType.READ,
            platform=Platform.MCP,
            parent=root,
            tool_name="weather.get_forecast",
            tool_server="weather-mcp",
            data_domain="weather",
            status=Status.TIMEOUT,
            duration_ms=30_000,
            cost_usd=_cost(b.rng, 0.002),
            error_code="MCP_TIMEOUT",
            error_message="upstream api.weather.gov did not respond within 30s",
            metadata={"attempt": 1},
        )
        b.emit(
            agent=FORECAST_AGENT,
            event_type=EventType.DATA_ACCESS,
            action=ActionType.READ,
            platform=Platform.OTHER,
            parent=failed,
            resource="https://api.weather.gov/gridpoints/forecast",
            data_domain="weather",
            status=Status.TIMEOUT,
            duration_ms=30_000,
            error_code="HTTP_504",
            error_message="gateway timeout",
        )

    call = b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.READ,
        platform=Platform.MCP,
        parent=root,
        tool_name="weather.get_forecast",
        tool_server="weather-mcp",
        data_domain="weather",
        duration_ms=_jitter(b.rng, 900),
        cost_usd=_cost(b.rng, 0.002),
        metadata={"attempt": 2 if degraded else 1, "regions": ["NE", "MW", "SO", "WE"]},
    )
    b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.DATA_ACCESS,
        action=ActionType.READ,
        platform=Platform.OTHER,
        parent=call,
        resource="https://api.weather.gov/gridpoints/forecast",
        data_domain="weather",
        duration_ms=_jitter(b.rng, 600),
    )
    return call


def _run_forecast(b: _RunBuilder, root: AgentEvent) -> AgentEvent:
    call = b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.EXECUTE,
        platform=Platform.LOCAL,
        parent=root,
        tool_name="forecast.run_model",
        data_domain="sales",
        duration_ms=_jitter(b.rng, 4200),
        cost_usd=_cost(b.rng, 0.018),
        metadata={"model": "prophet-weekly-v3", "horizon_weeks": 4},
    )
    b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.LLM_CALL,
        action=ActionType.EXECUTE,
        platform=Platform.LOCAL,
        parent=root,
        data_domain="sales",
        duration_ms=_jitter(b.rng, 3100),
        cost_usd=_cost(b.rng, 0.089),
        model="claude-sonnet-5",
        input_tokens=b.rng.randint(6_000, 12_000),
        output_tokens=b.rng.randint(700, 1_600),
        metadata={"purpose": "narrative_summary"},
    )
    return call


def _detect_anomaly(b: _RunBuilder, root: AgentEvent, drop_pct: float) -> AgentEvent:
    return b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.AGENT_STEP,
        action=ActionType.EXECUTE,
        parent=root,
        data_domain="sales",
        duration_ms=_jitter(b.rng, 250),
        metadata={
            "finding": "unusual_sales_drop",
            "drop_pct": drop_pct,
            "region": "MW",
            "category": "beverages",
        },
    )


def _investigate_inventory(b: _RunBuilder, anomaly: AgentEvent) -> AgentEvent:
    """Agent A -> Agent B -> MCP tool -> ERP."""
    handoff = b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.AGENT_HANDOFF,
        action=ActionType.DELEGATE,
        parent=anomaly,
        duration_ms=_jitter(b.rng, 120),
        metadata={"delegate_to": INVENTORY_AGENT[0], "reason": "unusual_sales_drop"},
    )
    child_start = b.emit(
        agent=INVENTORY_AGENT,
        event_type=EventType.AGENT_RUN_START,
        action=ActionType.START,
        parent=handoff,
        actor_id=FORECAST_AGENT[0],
        duration_ms=_jitter(b.rng, 60),
    )
    stock = b.emit(
        agent=INVENTORY_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.READ,
        platform=Platform.MCP,
        parent=child_start,
        tool_name="inventory.read_stock_levels",
        tool_server="inventory-mcp",
        data_domain="inventory",
        duration_ms=_jitter(b.rng, 1100),
        cost_usd=_cost(b.rng, 0.007),
    )
    b.emit(
        agent=INVENTORY_AGENT,
        event_type=EventType.DATA_ACCESS,
        action=ActionType.READ,
        platform=Platform.OTHER,
        parent=stock,
        resource="erp://inventory/stock_levels",
        data_domain="inventory",
        duration_ms=_jitter(b.rng, 800),
        metadata={"skus_checked": b.rng.randint(40, 180)},
    )
    b.emit(
        agent=INVENTORY_AGENT,
        event_type=EventType.AGENT_RUN_END,
        action=ActionType.FINISH,
        parent=child_start,
        duration_ms=_jitter(b.rng, 40),
        metadata={"conclusion": "stockout_in_MW_beverages"},
    )
    return handoff


def _publish_report(b: _RunBuilder, root: AgentEvent) -> AgentEvent:
    return b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.EXECUTE,
        parent=root,
        tool_name="reporting.render_draft",
        data_domain="reporting",
        duration_ms=_jitter(b.rng, 900),
        cost_usd=_cost(b.rng, 0.003),
        metadata={"artifact": "weekly_forecast.md", "published": False},
    )


def _end_run(b: _RunBuilder, root: AgentEvent, status: Status = Status.SUCCESS) -> AgentEvent:
    return b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.AGENT_RUN_END,
        action=ActionType.FINISH,
        parent=root,
        status=status,
        duration_ms=_jitter(b.rng, 50),
        error_code=None if status is Status.SUCCESS else "RUN_INCOMPLETE",
        error_message=None if status is Status.SUCCESS else "run ended with unresolved findings",
    )


# -- run variants ---------------------------------------------------------


def _build_standard_run(b: _RunBuilder, week_of: str, *, degraded: bool) -> None:
    root = _start_run(b, week_of)
    _query_sales(b, root)
    _query_promotions(b, root)
    _weather(b, root, degraded=degraded)
    _run_forecast(b, root)
    anomaly = _detect_anomaly(b, root, drop_pct=round(b.rng.uniform(8.0, 14.0), 1))
    _investigate_inventory(b, anomaly)
    _publish_report(b, root)
    # A degraded run still ends successfully: the retry recovered. Run status
    # follows the terminal event, not any failure inside it.
    _end_run(b, root, Status.SUCCESS)


def _build_violation_run(b: _RunBuilder, week_of: str) -> None:
    """The interesting one: a read-only agent overstepping in four ways."""
    root = _start_run(b, week_of)
    _query_sales(b, root)
    _query_promotions(b, root)
    _weather(b, root)
    _run_forecast(b, root)
    anomaly = _detect_anomaly(b, root, drop_pct=31.4)

    # (1) unauthorised domain: reaches into HR to "explain" the drop by staffing
    hr_call = b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.READ,
        platform=Platform.MCP,
        parent=anomaly,
        tool_name="hr.query_headcount",
        tool_server="snowflake-mcp",
        data_domain="hr",
        duration_ms=_jitter(b.rng, 800),
        cost_usd=_cost(b.rng, 0.009),
        metadata={"rationale": "correlate store staffing with sales drop"},
    )
    b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.DATA_ACCESS,
        action=ActionType.READ,
        platform=Platform.SNOWFLAKE,
        parent=hr_call,
        resource="HR.PUBLIC.DIM_EMPLOYEE",
        data_domain="hr",
        duration_ms=_jitter(b.rng, 600),
        cost_usd=_cost(b.rng, 0.014),
    )

    _investigate_inventory(b, anomaly)

    # (2) retry storm against a flaky tool -> tool-call volume anomaly
    for attempt in range(1, 9):
        b.emit(
            agent=FORECAST_AGENT,
            event_type=EventType.TOOL_CALL,
            action=ActionType.READ,
            platform=Platform.MCP,
            parent=anomaly,
            tool_name="inventory.read_shipments",
            tool_server="inventory-mcp",
            data_domain="logistics",
            status=Status.FAILURE if attempt < 8 else Status.SUCCESS,
            duration_ms=_jitter(b.rng, 2200),
            cost_usd=_cost(b.rng, 0.006),
            error_code="ERP_RATE_LIMITED" if attempt < 8 else None,
            error_message="429 from ERP shipment API" if attempt < 8 else None,
            metadata={"attempt": attempt},
        )

    # (3) + (4) read-only agent calls a mutating, approval-gated tool.
    # The control plane records the attempt even though the gateway denied it.
    b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.WRITE,
        platform=Platform.MCP,
        parent=anomaly,
        tool_name="inventory.adjust_stock",
        tool_server="inventory-mcp",
        resource="erp://inventory/stock_levels",
        data_domain="inventory",
        status=Status.DENIED,
        duration_ms=_jitter(b.rng, 150),
        error_code="TOOL_NOT_PERMITTED",
        error_message="gateway rejected write from read-only principal",
        metadata={
            "requested_change": {"sku": "BEV-1042", "delta": 500},
            "rationale": "auto-correct suspected stockout",
        },
    )

    # An expensive re-forecast pushes the run over its cost ceiling.
    b.emit(
        agent=FORECAST_AGENT,
        event_type=EventType.LLM_CALL,
        action=ActionType.EXECUTE,
        parent=anomaly,
        data_domain="sales",
        duration_ms=_jitter(b.rng, 9500),
        cost_usd=2.85,
        model="claude-opus-5",
        input_tokens=140_000,
        output_tokens=9_400,
        metadata={"purpose": "root_cause_analysis", "escalated_model": True},
    )

    _publish_report(b, root)
    _end_run(b, root, Status.FAILURE)


def _build_shadow_run(b: _RunBuilder, week_of: str) -> None:
    """An agent nobody registered, quietly reading pricing and sales data."""
    root = b.emit(
        agent=SHADOW_AGENT,
        event_type=EventType.AGENT_RUN_START,
        action=ActionType.START,
        actor_id="svc_pricing_batch",
        actor_type=ActorType.SERVICE,
        duration_ms=40,
        metadata={"week_of": week_of, "trigger": "manual"},
    )
    call = b.emit(
        agent=SHADOW_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.READ,
        platform=Platform.MCP,
        parent=root,
        tool_name="sales.query_history",
        tool_server="snowflake-mcp",
        data_domain="sales",
        duration_ms=_jitter(b.rng, 1500),
        cost_usd=_cost(b.rng, 0.011),
    )
    b.emit(
        agent=SHADOW_AGENT,
        event_type=EventType.DATA_ACCESS,
        action=ActionType.READ,
        platform=Platform.SNOWFLAKE,
        parent=call,
        resource="SALES.PUBLIC.FCT_ORDERS",
        data_domain="sales",
        duration_ms=_jitter(b.rng, 900),
        cost_usd=_cost(b.rng, 0.022),
    )
    b.emit(
        agent=SHADOW_AGENT,
        event_type=EventType.AGENT_RUN_END,
        action=ActionType.FINISH,
        parent=root,
        duration_ms=30,
    )


def _build_governed_publish_run(b: _RunBuilder, week_of: str) -> None:
    """The write path done properly: a linked human approval, then the write.

    Present so the approval rule is demonstrably capable of passing, not just
    firing.
    """
    root = b.emit(
        agent=PUBLISHER_AGENT,
        event_type=EventType.AGENT_RUN_START,
        action=ActionType.START,
        actor_id=ANALYST,
        actor_type=ActorType.HUMAN,
        duration_ms=40,
        metadata={"week_of": week_of, "trigger": "manual:publish-weekly"},
    )
    b.emit(
        agent=PUBLISHER_AGENT,
        event_type=EventType.APPROVAL,
        action=ActionType.APPROVE,
        parent=root,
        actor_id=APPROVER,
        actor_type=ActorType.HUMAN,
        duration_ms=_jitter(b.rng, 200),
        metadata={
            "approves_tool": "reporting.publish",
            "approver_role": "analytics_lead",
            "ticket": f"CHG-{week_of.replace('-', '')}",
        },
    )
    b.emit(
        agent=PUBLISHER_AGENT,
        event_type=EventType.TOOL_CALL,
        action=ActionType.WRITE,
        platform=Platform.MCP,
        parent=root,
        tool_name="reporting.publish",
        tool_server="reporting-mcp",
        resource="reporting://weekly/exec",
        data_domain="reporting",
        duration_ms=_jitter(b.rng, 1100),
        cost_usd=_cost(b.rng, 0.004),
        metadata={"artifact": "weekly_forecast.md", "audience": "exec-weekly"},
    )
    b.emit(
        agent=PUBLISHER_AGENT,
        event_type=EventType.AGENT_RUN_END,
        action=ActionType.FINISH,
        parent=root,
        duration_ms=30,
    )


_VARIANT_BUILDERS = {
    "clean": lambda b, week: _build_standard_run(b, week, degraded=False),
    "degraded": lambda b, week: _build_standard_run(b, week, degraded=True),
    "violation": _build_violation_run,
    "shadow": _build_shadow_run,
    "governed_publish": _build_governed_publish_run,
}

# Weekly runs telling a story: routine weeks, a governed publish, a flaky
# upstream, an unregistered agent appearing, then the week the agent oversteps.
DEFAULT_SCHEDULE: tuple[Variant, ...] = (
    "clean",
    "clean",
    "governed_publish",
    "degraded",
    "clean",
    "shadow",
    "clean",
    "degraded",
    "violation",
)


@dataclass(slots=True)
class SyntheticSource:
    """`EventSource` over the scripted forecast scenario."""

    seed: int = 20260810
    start: datetime = datetime(2026, 6, 22, 6, 0, tzinfo=UTC)
    schedule: tuple[Variant, ...] = DEFAULT_SCHEDULE
    tenant_id: str = DEFAULT_TENANT_ID

    @property
    def name(self) -> str:
        return ADAPTER_NAME

    def fetch(self, since: datetime | None = None) -> Iterable[AgentEvent]:
        for event in self._generate():
            if since is None or event.timestamp >= since:
                yield event

    def _generate(self) -> Iterator[AgentEvent]:
        rng = Random(self.seed)
        for index, variant in enumerate(self.schedule):
            run_start = self.start + timedelta(weeks=index)
            week_of = run_start.date().isoformat()
            correlation_id = f"run-{week_of}-{variant}"
            builder = _RunBuilder(
                tenant_id=self.tenant_id,
                correlation_id=correlation_id,
                clock=run_start,
                rng=rng,
                session_id=f"sess-{week_of}",
            )
            _VARIANT_BUILDERS[variant](builder, week_of)
            yield from builder.events


def generate_events(
    seed: int = 20260810,
    schedule: tuple[Variant, ...] = DEFAULT_SCHEDULE,
    tenant_id: str = DEFAULT_TENANT_ID,
) -> list[AgentEvent]:
    """Convenience for tests and scripts."""
    return list(SyntheticSource(seed=seed, schedule=schedule, tenant_id=tenant_id).fetch())
