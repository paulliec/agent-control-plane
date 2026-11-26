# Current state

A resumability checkpoint: what works, what is deliberately missing, and whether
the repo is green. Read with `NEXT.md`, which says what is approved to build.

**Last verified:** 2026-11-26, on branch `feat/pilot-runner`.

## Milestone

**Tollgate 2C complete, plus the Snowflake native-attribution refresh.** Current
work is public-demo readiness — packaging what exists for a hosted Streamlit
demo and a code review. No new product capability.

## Baseline

Obtained by running `./scripts/verify.sh`:

| Gate | Result |
|---|---|
| `ruff format --check .` | clean |
| `ruff check .` | clean |
| `mypy --strict` (src + tests + scripts) | clean, 64 files |
| `pytest --cov` | **669 passed**, **97.62%** coverage (floor 96%) |
| Demo seed from committed fixtures | 166 events, 20 violations |
| CLI smoke (`stats`/`runs`/`violations`/`trace`/`evaluate`) | clean |

Coverage omits `dashboard/app.py` by design: Streamlit runs the script on its
own ScriptRunner thread and coverage.py attributes none of it back, so it
measures 0% even while the AppTest suite exercises every view. The logic it
would have measured lives in `services/dashboard.py` and `dashboard/presenters.py`,
both at 100%.

## Capabilities

- **Vendor-neutral core.** Three sources normalize into one `AgentEvent` and are
  governed by one rule set. No vendor type exists above `adapters/`.
- **Execution chains.** Parent/child reconstruction that handles a synthetic run
  envelope, MCP spans, and Snowflake's flat multi-root forest.
- **Policy engine.** Eight rules, severity-ranked, grouped findings (eight
  retries of a forbidden tool is one finding with `occurrences: 8`). Every rule
  emits a result on every run, so "no violations" stays distinguishable from
  "nobody checked".
- **Decision metadata.** Findings record what a gateway *would* have done
  (`would_block`). `PolicyResult.enforced` is rejected outright.
- **Tenancy.** `tenant_id` on every event, chain and policy result; required
  positional argument on every repository read.
- **Data-domain governance.** Controlled vocabulary in YAML, most-specific-match
  mapping from vendor objects, reserved non-declarable `unknown` domain.
- **Snowflake attribution.** Native `AGENT_TYPE` / `AGENTS_INFO` plus `QUERY_TAG`,
  with provenance preserved rather than flattened.
- **Cost attribution** across sources, labelled estimated everywhere, with
  credits-vs-dollars stated wherever a dollar appears.
- **CLI** (`acp`) — the whole system is exercisable without Streamlit.
- **Streamlit console** — Overview, Runs, Findings, Trace, Data domains & cost;
  needs-attention panel with drill-downs into pre-filtered findings.
- **Self-seeding public demo.** The console builds the deterministic dataset on
  first load when it finds no events. See "Public demo" below.

## Demo sources

All three are credential-free and deterministic.

| Source | Shape | Backing |
|---|---|---|
| `synthetic` | Generated *Weekly Sales Forecast Agent* scenario, nine runs | `adapters/synthetic.py`, fixed seed `20260810` |
| `mcp` | MCP gateway call log | `data/samples/mcp_tool_calls.jsonl` |
| `snowflake` | `ACCOUNT_USAGE` join (query history, access history, Cortex usage) | `data/samples/snowflake_activity.jsonl` |

A fresh demo holds 143 synthetic, 10 MCP and 13 Snowflake events. The Overview
charts the generated scenario separately from the integration extracts, because
the size of a scenario we authored is a choice rather than evidence.

## Public demo

`ACP_DEMO_BOOTSTRAP` (default **on**) lets the console seed itself when the
tenant has no events: `demo.ensure_demo_data` checks a tenant-scoped `COUNT(*)`
and builds the dataset only if it is zero. Idempotent — a populated database is
left untouched, and nothing is ever reset or deleted. `acp seed --reset` remains
the only thing that removes data.

Streamlit Community Cloud has an ephemeral filesystem, so the demo database is
rebuilt after a restart. That is acceptable and invisible: the seed is pinned, so
the regenerated dataset is identical.

Set `ACP_DEMO_BOOTSTRAP=false` for any deployment reading real telemetry. There
an empty database means ingestion is broken, and synthesizing demo rows would
hide exactly that.

## Known limitations and debt

Accurate as of the date above. None of these are bugs; all are deliberate stops.

- **No live Snowflake extraction.** The adapter reads a committed fixture. It
  imports no connector and needs no credentials. Pointing it at a real
  `ACCOUNT_USAGE` export is the intended next step, not a rewrite.
- **Agentic-but-unidentified Snowflake activity is unrepresentable.**
  `AgentEvent.agent_id` is required, so a query that `AGENTS_INFO` marks as
  agentic with a null `agentId` cannot be stored as the agent event it is. The
  unknown-agent sentinel that would fix this is designed but **not** approved.
- **No active enforcement.** Decisions are evaluated intent. Nothing blocks.
- **Cost provenance is limited.** Snowflake reports credits; dollars come from a
  rate supplied at seed time ($3.00/credit in the demo), which is an assumption
  wearing the units of a measurement. Coverage is partial — not every event
  carries a cost figure — and the console states the share that does.
- **No declared-vs-observed discovery.** ACP sees what the telemetry reports.
  There is no inventory reconciliation, so "an agent exists but emitted nothing"
  is invisible.
- **No auth/authz.** Tenant isolation is a data-model property, not an access
  control system. The console's tenant selector trusts whoever opened the page.
  Nothing here is suitable for a shared SaaS deployment.
- **No retention or ruleset versioning.** Policy results are derived and
  re-evaluated wholesale; there is no record of which rule version produced a
  historical finding.
- **SQLite only.** Fine at this scale; the repository Protocol exists so a
  DuckDB/Postgres store is a new class rather than a refactor.

## Last meaningful work

1. Read-side query layer for the console (`services/dashboard.py`).
2. Streamlit console, five views.
3. Console documentation.
4. Needs-attention panel with drill-downs.
5. Snowflake native agent telemetry for attribution (`AGENT_TYPE` / `AGENTS_INFO`).
6. Project harness, self-seeding public demo, deployment readiness.
7. Development harness Phase 0/1.5: scaffolding plus the
   Phase 1.5 security fixes (`docs/HARNESS.md`). 176 of the 591 tests are
   harness tests. Coverage measures only the package, so the figure is
   unchanged.
8. *This change:* pilot runner (PR #2), implemented on `feat/pilot-runner`,
   never run live (`docs/HARNESS.md`, "Pilot runner"). 78 new tests: 254 of
   the 669 are harness tests.

## Branches

`main` is the integration branch; feature work happens on a branch and is not
merged by an agent. Current: `feat/pilot-runner`, local only, on top of
`demo-release-readiness` (`f0b5f57`, which includes the merged PR #1).
