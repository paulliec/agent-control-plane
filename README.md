# Agent Control Plane Lite

Observability and governance for AI agents and MCP tool usage, across platforms.

## The problem

Agents are being deployed faster than anyone can inventory them. Each platform —
Snowflake, Databricks, Azure AI Foundry, whatever MCP gateway a team stood up
last month — has its own log format, its own idea of identity, and its own
console. Nobody can answer basic questions across all of them:

- What agents exist, and which platform does each run on?
- Who or what identity invoked this agent?
- Which tools did it call, and which data domains did it touch?
- What did it cost?
- Did it succeed?
- Did it violate policy?
- Can I trace an action back through the agent/tool chain?

This project answers those questions from a single, vendor-neutral event model.
Adapters translate each platform's telemetry into that model; everything above
the adapter boundary is platform-agnostic.

```
Human
  └─ Forecast Agent
       └─ Weather MCP Tool
            └─ Weather API

Agent A
  └─ Agent B
       └─ MCP Tool
            └─ Snowflake
```

Both shapes are reconstructable from stored events.

## Status

**Tollgate 1.** Local/synthetic and MCP-log ingestion, SQLite persistence, a
rule-based policy engine, analytics, a CLI, and tests. No dashboard yet, no
cloud adapters yet. See `docs/ARCHITECTURE.md` for how the later adapters slot
in without touching the core.

## Architecture in one screen

```
       cli.py              dashboard/ (Tollgate 2)
          │                     │
          └────────┬────────────┘
                   ▼
   services/  ingestion · policy_engine · policy_rules · analytics
                   │
        ┌──────────┴───────────┐
        ▼                      ▼
  repositories/           adapters/
  (Protocol)              (Protocol)
  sqlite.py               synthetic.py · mcp.py
        │                      │
        └──────────┬───────────┘
                   ▼
              models/  (vendor-neutral: AgentEvent, Agent, Tool, PolicyResult)
```

Imports only ever point downward. `models/` imports nothing from the project;
no vendor SDK type is permitted above `adapters/`.

Full detail: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
Open decisions: [`docs/QUESTIONS_FOR_PAUL.md`](docs/QUESTIONS_FOR_PAUL.md).

## Install

Requires Python 3.12+ (developed on 3.13).

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

```bash
# macOS / Linux
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

No cloud credentials are needed for anything in this repo.

## Run the tests

```powershell
.venv\Scripts\python.exe -m pytest
.venv\Scripts\python.exe -m pytest --cov=agentops --cov-report=term-missing
```

## Generate sample data

Seeds the deterministic *Weekly Sales Forecast Agent* scenario plus the sample
MCP gateway log, then evaluates every policy over it:

```powershell
.venv\Scripts\agentops.exe seed --reset
```

or, without the console script:

```powershell
.venv\Scripts\python.exe scripts\seed_demo.py --reset
```

The scenario covers nine weekly runs: routine forecasts, a governed publish
(human approval linked to the write), two degraded runs where the weather MCP
server times out and the agent retries, a shadow agent nobody registered, and
one run where the forecast agent oversteps in four distinct ways.

## Explore from the CLI

```powershell
.venv\Scripts\agentops.exe stats                             # headline metrics
.venv\Scripts\agentops.exe runs                              # recent execution history
.venv\Scripts\agentops.exe violations                        # findings, worst first
.venv\Scripts\agentops.exe trace run-2026-08-17-violation    # one execution chain
.venv\Scripts\agentops.exe evaluate                          # re-run policies after a rule change
```

`trace` prints the reconstructed call tree:

```
run run-2026-08-17-violation  cost=$3.1074  agents=agt_forecast_weekly, agt_inventory_investigator

run                 agt_forecast_weekly          start  success
  tool              agt_forecast_weekly          sales.query_history [sales]  success $0.0149
    data            agt_forecast_weekly          SALES.PUBLIC.FCT_ORDERS [sales]  success $0.0323
  ...
  step              agt_forecast_weekly          execute [sales]  success
    tool            agt_forecast_weekly          hr.query_headcount [hr]  success $0.0102
      data          agt_forecast_weekly          HR.PUBLIC.DIM_EMPLOYEE [hr]  success $0.0162
    ->              agt_forecast_weekly          delegate  success
      run           agt_inventory_investigator   start  success
        tool        agt_inventory_investigator   inventory.read_stock_levels [inventory]  success $0.0086
    tool            agt_forecast_weekly          inventory.adjust_stock [inventory]  denied

violations
  [critical] read_only_violation: agt_forecast_weekly is configured read_only but
             attempted write via 'inventory.adjust_stock' once (detected by: action, tool_registry).
  [high    ] tool_not_allowlisted: agt_forecast_weekly called 'inventory.read_shipments'
             8 times; its tool_access scope (allowlist (6)) does not permit it.
  ...
```

Note the grouping: eight retries of a forbidden tool are **one** finding with
`occurrences: 8` in the evidence, not eight findings burying the critical one.

## Configuration

Governance lives in version-controlled YAML, not in the database:

- `config/agents.yaml` — agent inventory and each agent's governance envelope
- `config/tools.yaml` — tool inventory, including `is_mutating` and `requires_approval`

Access scope is always **two explicit fields**, a mode and a list:

```yaml
tool_access_mode: allowlist
allowed_tools: [sales.query_history, forecast.run_model]
```

An empty list under `allowlist` means *no tools allowed*. It never means
"unrestricted" — that requires `tool_access_mode: unrestricted`, and a list is
then rejected outright. Emptiness carries no security meaning anywhere in this
system; see the rationale in `docs/ARCHITECTURE.md`.

Runtime settings come from env vars prefixed `AGENTOPS_` (or a `.env` file):
`AGENTOPS_DB_PATH`, `AGENTOPS_REGISTRY_DIR`, `AGENTOPS_LOG_LEVEL`,
`AGENTOPS_LOG_JSON`, `AGENTOPS_DEFAULT_MAX_COST_USD`,
`AGENTOPS_DEFAULT_MAX_TOOL_CALLS`.

## Policies

| Policy | Severity | Fires when |
|---|---|---|
| `read_only_violation` | critical | A read-only agent attempts a write — detected from the declared action *or* the tool registry's `is_mutating` |
| `unauthorized_data_domain` | high | An agent touches a data domain its `data_access` scope does not permit |
| `tool_not_allowlisted` | high | An agent calls a tool its `tool_access` scope does not permit |
| `missing_human_approval` | high | An approval-gated action ran with no *linked* approval event |
| `cost_threshold_exceeded` | medium | Per-agent spend in one run exceeds its ceiling |
| `excessive_tool_calls` | medium | Per-agent tool calls in one run exceed its ceiling |
| `unregistered_agent` | medium | Activity from an agent that is not in the registry |

Two behaviours worth knowing:

- **A blocked attempt is still a violation.** If the gateway denies a write, the
  attempt is recorded as a finding. The gateway working is not the same as the
  agent behaving.
- **Every rule emits a result on every run** — its findings, or one PASS row
  proving it ran. "No violations" stays distinguishable from "nobody checked".

Adding a rule means writing a class with `name`, `severity`, `description` and
`evaluate(chain, ctx)`, then adding it to `default_rules()`. No registration
machinery.

## Layout

```
src/agentops/
  models/        vendor-neutral domain model
  adapters/      one module per source; Protocol in base.py
  repositories/  persistence Protocol + SQLite implementation
  services/      ingestion, policy engine, rules, analytics
  bootstrap.py   composition root — the only place concrete types are wired
  cli.py
config/          agent + tool registries (governance intent)
data/samples/    committed fixture input (sample MCP gateway log)
docs/            ARCHITECTURE.md, QUESTIONS_FOR_PAUL.md
tests/
```

## License

MIT.
