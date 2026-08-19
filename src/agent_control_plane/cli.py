"""Command line entry point.

Exists so the whole system is exercisable without Streamlit — the dashboard is
a view, not the product.

    agent_control_plane seed --reset
    agent_control_plane stats
    agent_control_plane runs
    agent_control_plane trace run-2026-08-17-violation
    agent_control_plane violations
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agent_control_plane.adapters.base import EventSource
from agent_control_plane.adapters.mcp import MCPCallLogSource
from agent_control_plane.adapters.synthetic import SyntheticSource
from agent_control_plane.bootstrap import ControlPlane, build_control_plane
from agent_control_plane.config import load_settings
from agent_control_plane.logging import configure_logging
from agent_control_plane.models.enums import EventType

_TREE_MARK = {
    EventType.AGENT_RUN_START: "run",
    EventType.AGENT_RUN_END: "end",
    EventType.AGENT_HANDOFF: "->",
    EventType.TOOL_CALL: "tool",
    EventType.DATA_ACCESS: "data",
    EventType.LLM_CALL: "llm",
    EventType.APPROVAL: "ok?",
    EventType.AGENT_STEP: "step",
}


def _default_mcp_log(cp: ControlPlane) -> Path:
    return cp.settings.data_dir / "samples" / "mcp_tool_calls.jsonl"


# -- commands -------------------------------------------------------------


def cmd_seed(cp: ControlPlane, args: argparse.Namespace) -> int:
    sources: list[EventSource] = [SyntheticSource(seed=args.seed, tenant_id=cp.tenant_id)]
    mcp_log = Path(args.mcp_log) if args.mcp_log else _default_mcp_log(cp)
    if mcp_log.exists():
        sources.append(
            MCPCallLogSource(
                mcp_log, tenant_id=cp.tenant_id, domains=cp.registry.data_domains
            )
        )
    else:
        print(f"! no MCP log at {mcp_log}, skipping that source")

    for result in cp.ingestion.ingest_all(sources):
        print(
            f"{result.source:<10} fetched={result.fetched:<4} written={result.written:<4} "
            f"duplicates={result.duplicates:<4} runs={len(result.correlation_ids):<3} "
            f"violations={result.violations}"
        )
    return 0


def cmd_evaluate(cp: ControlPlane, _: argparse.Namespace) -> int:
    results, violations = cp.ingestion.evaluate_everything()
    print(f"evaluated {results} policy result(s), {violations} violation(s)")
    return 0


def cmd_stats(cp: ControlPlane, _: argparse.Namespace) -> int:
    o = cp.analytics.overview()
    rows = [
        ("runs", o.total_runs),
        ("events", o.total_events),
        ("successful runs", o.successful_runs),
        ("failed runs", o.failed_runs),
        ("total cost usd", f"{o.total_cost_usd:.4f}"),
        ("tool calls", f"{o.total_tool_calls} ({o.failed_tool_calls} not ok)"),
        ("unique agents", o.unique_agents),
        ("unregistered agents", o.unregistered_agents),
        ("policy violations", o.violation_count),
        ("critical violations", o.critical_violations),
    ]
    for label, value in rows:
        print(f"{label:<22} {value}")

    print("\nevents by platform")
    for platform, count in cp.analytics.events_by_platform().items():
        print(f"  {platform:<14} {count}")

    print("\ntop tools")
    for usage in cp.analytics.top_tools(8):
        print(
            f"  {usage.tool_name:<30} calls={usage.call_count:<4} "
            f"failed={usage.failure_count:<3} cost=${usage.cost_usd:.4f}"
        )

    print("\ncost by agent")
    for agent in cp.analytics.cost_by_agent(8):
        print(
            f"  {agent.agent_id:<32} runs={agent.run_count:<3} "
            f"cost=${agent.cost_usd:.4f} violations={agent.violation_count}"
        )
    return 0


def cmd_runs(cp: ControlPlane, args: argparse.Namespace) -> int:
    for run in cp.analytics.recent_runs(args.limit):
        severity = run.max_severity.value if run.max_severity else "-"
        print(
            f"{run.started_at:%Y-%m-%d %H:%M}  {run.correlation_id:<34} "
            f"{run.status_label:<8} events={run.event_count:<4} "
            f"cost=${run.cost_usd:<8.4f} violations={run.violation_count:<3} max={severity}"
        )
    return 0


def cmd_trace(cp: ControlPlane, args: argparse.Namespace) -> int:
    chain = cp.analytics.chain(args.correlation_id)
    if chain is None:
        print(f"no such run: {args.correlation_id}", file=sys.stderr)
        return 1

    print(f"run {chain.correlation_id}  cost=${chain.total_cost_usd:.4f}  "
          f"agents={', '.join(chain.agent_ids)}\n")
    for depth, event in chain.walk():
        mark = _TREE_MARK.get(event.event_type, "?")
        label = event.tool_name or event.resource or event.action.value
        cost = f" ${event.estimated_cost_usd:.4f}" if event.estimated_cost_usd else ""
        domain = f" [{event.data_domain}]" if event.data_domain else ""
        # Pad the indent+marker as one unit so the agent column stays aligned
        # however deep the tree goes.
        prefix = f"{'  ' * depth}{mark}"
        print(
            f"{prefix:<20}{event.agent_id:<28} {label}{domain}"
            f"  {event.status.value}{cost}"
        )

    violations = cp.repository.list_policy_results(
        cp.tenant_id, correlation_id=args.correlation_id, violations_only=True
    )
    if violations:
        violations.sort(key=lambda v: (-v.severity.rank, v.policy_name))
        print("\nviolations")
        for v in violations:
            print(f"  [{v.severity.value:<8}] {v.policy_name}: {v.explanation}")
    return 0


def cmd_violations(cp: ControlPlane, args: argparse.Namespace) -> int:
    for v in cp.analytics.violations(args.limit):
        print(f"[{v.severity.value:<8}] {v.policy_name:<26} {v.correlation_id}")
        print(f"           {v.explanation}")
    return 0


# -- wiring ---------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent_control_plane", description=__doc__)
    parser.add_argument("--db", help="override the SQLite path")
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--tenant", default=None, help="tenant to operate on")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="generate and ingest sample data")
    seed.add_argument("--seed", type=int, default=20260810)
    seed.add_argument("--mcp-log", default=None)
    seed.add_argument("--reset", action="store_true", help="delete the database first")
    seed.set_defaults(func=cmd_seed)

    evaluate = sub.add_parser("evaluate", help="re-run all policies over stored events")
    evaluate.set_defaults(func=cmd_evaluate)

    stats = sub.add_parser("stats", help="headline metrics")
    stats.set_defaults(func=cmd_stats)

    runs = sub.add_parser("runs", help="recent execution history")
    runs.add_argument("--limit", type=int, default=20)
    runs.set_defaults(func=cmd_runs)

    trace = sub.add_parser("trace", help="print one execution chain")
    trace.add_argument("correlation_id")
    trace.set_defaults(func=cmd_trace)

    violations = sub.add_parser("violations", help="policy violations, worst first")
    violations.add_argument("--limit", type=int, default=50)
    violations.set_defaults(func=cmd_violations)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    configure_logging(args.log_level or settings.log_level, settings.log_json)

    db_path = Path(args.db) if args.db else settings.db_path
    if getattr(args, "reset", False) and db_path.exists():
        db_path.unlink()
        print(f"removed {db_path}")

    with build_control_plane(settings, db_path=db_path, tenant_id=args.tenant) as cp:
        return int(args.func(cp, args))


if __name__ == "__main__":
    raise SystemExit(main())
