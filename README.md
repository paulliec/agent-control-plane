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

**Tollgate 2, in progress.** Local/synthetic and MCP-log ingestion, SQLite
persistence, a rule-based policy engine, analytics, a CLI, and tests. Since
Tollgate 1: tenancy on every event and every read, a controlled vocabulary for
data domains, and decision metadata on rules so a finding records what a gateway
*would* have done. Still observe-only. No dashboard yet, no cloud adapters yet.
See `docs/ARCHITECTURE.md` for how the later adapters slot in without touching
the core.

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
              models/  (vendor-neutral: AgentEvent, Agent, Tool, DataDomain, PolicyResult)
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
.venv\Scripts\python.exe -m pytest --cov          # with the coverage gate
```

## Quality gates

The same four commands run locally and in CI, configured once in
`pyproject.toml` so they cannot disagree:

```powershell
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m mypy --strict         # src + tests + scripts
.venv\Scripts\python.exe -m pytest --cov          # fails under 96%
```

`.github/workflows/ci.yml` runs those on every pull request and every push to
`main`, then seeds the demo and exercises the CLI. One job, Python 3.12, no
matrix — nothing here needs credentials or network, which is what keeps it fast.

## Generate sample data

Seeds the deterministic *Weekly Sales Forecast Agent* scenario plus the sample
MCP gateway log, then evaluates every policy over it:

```powershell
.venv\Scripts\acp.exe seed --reset
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
.venv\Scripts\acp.exe stats                             # headline metrics
.venv\Scripts\acp.exe runs                              # recent execution history
.venv\Scripts\acp.exe violations                        # findings, worst first
.venv\Scripts\acp.exe trace run-2026-08-17-violation    # one execution chain
.venv\Scripts\acp.exe evaluate                          # re-run policies after a rule change
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
- `config/data_domains.yaml` — the controlled data-domain vocabulary and the
  rules that map vendor objects onto it

Access scope is always **two explicit fields**, a mode and a list:

```yaml
tool_access_mode: allowlist
allowed_tools: [sales.query_history, forecast.run_model]
```

An empty list under `allowlist` means *no tools allowed*. It never means
"unrestricted" — that requires `tool_access_mode: unrestricted`, and a list is
then rejected outright. Emptiness carries no security meaning anywhere in this
system; see the rationale in `docs/ARCHITECTURE.md`.

Runtime settings come from env vars prefixed `ACP_` (or a `.env` file):
`ACP_TENANT_ID`, `ACP_DB_PATH`, `ACP_REGISTRY_DIR`, `ACP_LOG_LEVEL`,
`ACP_LOG_JSON`, `ACP_DEFAULT_MAX_COST_USD`,
`ACP_DEFAULT_MAX_TOOL_CALLS`.

## Data domains

A data domain is a governance term (`sales`, `hr`, `logistics`), not whatever
string a vendor happened to log. `config/data_domains.yaml` declares the
vocabulary and the mapping rules that get you there:

```yaml
domains:
  - name: hr
    sensitivity: restricted        # public | internal | confidential | restricted
    owner: people-ops@contoso.com

mappings:
  - {platform: snowflake, pattern: "HR.*.*", domain: hr}
  - {platform: mcp, pattern: "ERP://SHIPMENTS/*", domain: logistics}
```

The pipeline is always **vendor object → mapping rule → controlled domain**.
Patterns are case-insensitive globs over each platform's natural identifier, and
the **most specific match wins** — fewest wildcards, then longest — so adding a
narrow rule never means reordering the file. A gateway's own `data_domain` field
is treated as a *claim*: it survives only if it is in the vocabulary, and a
mapping rule always beats it.

When nothing matches, the answer is the reserved `unknown` domain and a policy
finding — never the vendor's string. `unknown` is deliberately not declarable,
so no agent can be granted access to it. An unclassified object is a governance
gap of *ours*, which is why `unknown_data_domain` is medium and never blocks,
and why `unauthorized_data_domain` stays quiet about it rather than charging the
agent with a scope breach nobody could have satisfied.

## Tenancy

Every event, chain and policy result carries a `tenant_id`, and **every
repository read takes one as a required argument** — not an optional filter with
a default, because a forgettable tenant filter is one someone will forget.
Chains reject mixed-tenant batches, and event fingerprints include the tenant,
so the same event id in two tenants is two events.

One `ControlPlane` serves one tenant. Serving several means building several,
which makes cross-tenant access an explicit act. Registry entries are scoped the
same way: an entry with no `tenant_id` belongs to the default tenant, so a second
tenant starts with an empty vocabulary rather than inheriting yours.

## Policies

| Policy | Severity | Decision | Fires when |
|---|---|---|---|
| `read_only_violation` | critical | deny | A read-only agent attempts a write — detected from the declared action *or* the tool registry's `is_mutating` |
| `unauthorized_data_domain` | high | deny | An agent touches a *classified* data domain its `data_access` scope does not permit |
| `tool_not_allowlisted` | high | deny | An agent calls a tool its `tool_access` scope does not permit |
| `missing_human_approval` | high | require approval | An approval-gated action ran with no *linked* approval event |
| `cost_threshold_exceeded` | medium | deny | Per-agent spend in one run exceeds its ceiling |
| `excessive_tool_calls` | medium | deny | Per-agent tool calls in one run exceed its ceiling |
| `unregistered_agent` | medium | allow | Activity from an agent that is not in the registry |
| `unknown_data_domain` | medium | allow | Data was touched under a domain no mapping rule classifies, or one outside the vocabulary |

Three behaviours worth knowing:

- **A blocked attempt is still a violation.** If the gateway denies a write, the
  attempt is recorded as a finding. The gateway working is not the same as the
  agent behaving.
- **Every rule emits a result on every run** — its findings, or one PASS row
  proving it ran. "No violations" stays distinguishable from "nobody checked".
- **The decision column is intent, not action.** This system observes; it does
  not enforce. A violation whose rule decides `deny` or `require_approval` sets
  `would_block`, which answers "what would a gateway have done here?" without
  anyone having claimed it did. `PolicyResult.enforced` exists for the day
  enforcement lands and is rejected outright if set today — an audit trail that
  says something was blocked when nothing was is worse than no audit trail.

Adding a rule means writing a class with `name`, `severity`, `description`,
`decision` and `evaluate(chain, ctx)`, then adding it to `default_rules()`. No
registration machinery.

## Layout

```
src/agent_control_plane/
  models/        vendor-neutral domain model (incl. data_domain.py)
  adapters/      one module per source; Protocol in base.py
  repositories/  persistence Protocol + SQLite implementation
  services/      ingestion, policy engine, rules, analytics
  bootstrap.py   composition root — the only place concrete types are wired
  constants.py   shared literals (default tenant, the `unknown` domain)
  cli.py
config/          agent, tool + data-domain registries (governance intent)
data/samples/    committed fixture input (sample MCP gateway log)
docs/            ARCHITECTURE.md, QUESTIONS_FOR_PAUL.md
.github/         CI workflow
tests/
```

## License

MIT.
