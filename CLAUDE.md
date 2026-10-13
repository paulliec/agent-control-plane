# CLAUDE.md — operating contract

Durable rules for working in this repo. Read `docs/CURRENT_STATE.md` for where
the project is and `docs/NEXT.md` for what is approved to build. Architecture
detail lives in `docs/ARCHITECTURE.md`; decisions and their costs in
`docs/QUESTIONS_FOR_PAUL.md`.

## What ACP is

Observability and governance for AI agents and MCP tool usage across platforms.
It answers, from one vendor-neutral event model: what agents exist, who invoked
them, which tools and data domains they touched, what it cost, whether they
violated policy, and how to trace any of it back through the chain.

It is **observe → evaluate → report**. Not enforcement. See "Decisions are
intent" below.

## Architecture at a glance

```
MCP ──────────┐
Snowflake ────┼──> AgentEvent → Policy → Persistence/Analytics → CLI / Console
Synthetic ────┘
```

```
cli.py  ·  dashboard/            presentation
services/                        ingestion · policy_engine · policy_rules · analytics · dashboard
repositories/ (Protocol)  ·  adapters/ (Protocol)
models/                          vendor-neutral, imports nothing from the project
bootstrap.py                     composition root — the only place concrete types are wired
```

Imports point downward only. No vendor SDK type is permitted above `adapters/`.

## Rules

**Source-specific knowledge stops at the adapter.** A `QUERY_TAG` parser, an
`AGENTS_INFO` shape, an MCP span layout — all of that lives in `adapters/` and
nowhere above it. If a service needs to know which vendor produced a row, the
normalization is wrong.

**`AgentEvent` is the contract.** Every source normalizes into it; everything
above the adapter boundary speaks only it. Changing its semantics is a
cross-cutting change, not a local one.

**Policy semantics are source-neutral.** One rule set governs every source. A
rule that only makes sense for Snowflake belongs in the adapter's normalization
or not at all. Do not add new policy rules without approval (`docs/NEXT.md`).

**Tenant isolation is non-negotiable.** Every repository read takes `tenant_id`
as a required positional argument — never an optional filter with a default.
One `ControlPlane` serves one tenant; serving several means building several.
`list_tenants()` is the single deliberate unscoped read, for admin tooling.
Chains reject mixed-tenant batches; fingerprints include the tenant.

**Preserve attribution provenance.** When a source tells us *how* it knew who
the agent was — native `AGENT_TYPE`/`AGENTS_INFO`, a `QUERY_TAG`, a gateway
client block — that provenance is recorded, not flattened into a bare id. A
reviewer must be able to ask "how do you know?" and get an answer.

**Never fabricate unavailable data.** No invented identity, token counts or
cost. Snowflake reports *credits*; dollars require a rate someone supplied, and
every dollar figure says so. Absent data stays absent — `None`, not `0`, and
not a plausible-looking estimate. A number that looks measured but was guessed
is worse than a blank.

**Unknown domain ≠ unauthorized domain.** `unknown_data_domain` means *we* never
wrote a mapping rule for the object touched; nobody could have been allowlisted
for it, so it is advisory, never blocks, and is our backlog. `unauthorized_data_domain`
means the agent went outside its allowlist — that is agent conduct. Keep the two
visibly distinct everywhere they surface.

**Actor identity ≠ logical agent identity.** `actor_id` is the principal or
service account that ran the work; `agent_id` is the logical agent. A Snowflake
service user is not an agent name. Do not collapse them.

**Snowflake attribution context is not automatically an event tree.** Native
`AGENTS_INFO` says agentic activity happened; it does not supply parentage. Do
not synthesize a chain from it. Snowflake has no run-envelope record, so its
chains are legitimately multi-root forests and multi-object fanout is real.

**Decisions are intent, not action.** A rule's `deny` / `require_approval` sets
`would_block`, answering "what would a gateway have done?". `PolicyResult.enforced`
is rejected outright today. An audit trail that claims something was blocked
when nothing was is worse than no audit trail.

**Empty carries no security meaning.** Access scope is always two fields — a
mode and a list. `allowlist` + `[]` is deny-all; unrestricted requires
`mode: unrestricted`, and a list alongside it is a validation error.

**The dashboard owns no business logic.** `dashboard/app.py` is presentation
only: no SQL, no policy logic, no domain classification, no chain construction.
Numbers come from `AnalyticsService` or `DashboardService`. If a view needs
something they do not expose, add the method to the service. Row shaping goes in
`dashboard/presenters.py`, which does not import Streamlit.

**Go through repositories and services, never raw SQLite.** Presentation code
does not open connections or write SQL. Note the console opens a `ControlPlane`
per rerun and must *not* be wrapped in `st.cache_resource`: a sqlite3 connection
belongs to the thread that opened it, and Streamlit reruns on a pool thread.
Relaxing `check_same_thread` to suit the UI is the wrong direction for the
dependency to point.

## Quality gate

```
./scripts/verify.sh
```

Format, lint, `mypy --strict`, `pytest --cov`, deterministic demo seed, CLI
smoke. All thresholds live in `pyproject.toml`. Do not lower the coverage floor
to make a change fit. CI keeps the same commands as separate named steps so a
failure is annotated by name.

## Commits and branches

- Work on a branch. Do not merge to `main`; do not rewrite history.
- Coherent commits, plain messages: `fix retry logic`, not a changelog entry.
- Delete dead code rather than commenting it out — that is what git is for.
- Comments explain *why*. Skip the obvious ones.
