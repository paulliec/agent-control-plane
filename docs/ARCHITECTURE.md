# Architecture

Status: Tollgate 2, in progress. Local/synthetic + MCP-log ingestion, SQLite
persistence, rule-based policy engine, analytics, CLI. Tenancy, a controlled
data-domain vocabulary and decision metadata landed since Tollgate 1; the system
is still observe-only. No dashboard, no cloud adapters yet.

## Principle

One vendor-neutral domain model in the middle. Everything platform-specific is
pushed to the edges, behind a Protocol.

```
       cli.py                    dashboard/  (Tollgate 2)
          │                           │
          └─────────────┬─────────────┘
                        │
                        ▼
     services/   ingestion · policy_engine · policy_rules · analytics
                        │
          ┌─────────────┴──────────────┐
          ▼                            ▼
   repositories/base.py          adapters/base.py
      (Protocol)                    (Protocol)
          │                            │
    sqlite.py                  synthetic.py · mcp.py
          │                     (future: snowflake · databricks · foundry)
          └─────────────┬──────────────┘
                        ▼
                    models/
   AgentEvent · Agent · Tool · DataDomain · PolicyResult
```

Two rules keep this honest:

1. **Imports point downward only.** `models/` imports nothing else from the
   project. `services/` never imports a concrete repository or adapter — only
   the Protocols and whatever `bootstrap.py` injects.
2. **No vendor type crosses the adapter boundary.** A Snowflake `QUERY_ID`, a
   Databricks run object, an MCP JSON-RPC frame — all stop inside their adapter.
   `AgentEvent` uses `extra="forbid"` so a leaked vendor field is a loud test
   failure, not a silent schema drift.

`bootstrap.py` is the only place concrete classes are chosen. The CLI, the tests
and (later) the dashboard all call `build_control_plane()`.

## Domain model

### `AgentEvent`

The atom. One observed action by one agent. Frozen, timezone-aware, closed
enums.

| Group | Fields | Why |
|---|---|---|
| Tenancy | `tenant_id` | Which organisation's data this is; required, never inferred |
| Identity | `event_id`, `timestamp`, `platform`, `source_adapter`, `ingested_at` | Provenance and replay |
| Principal | `agent_id`, `agent_name`, `actor_id`, `actor_type` | *Who* triggered this step |
| Action | `event_type`, `action`, `tool_name`, `tool_server`, `resource`, `data_domain` | What was done, to what |
| Outcome | `status`, `error_code`, `error_message` | Success, failure, **denied**, timeout |
| Economics | `duration_ms`, `estimated_cost_usd`, `model`, `input_tokens`, `output_tokens` | Cost attribution |
| Chain | `correlation_id`, `parent_event_id`, `session_id` | Trace reconstruction |
| Extras | `metadata` | Adapter-specific detail that hasn't earned a column |

Two deliberate splits:

- **`event_type` vs `action`.** `event_type` is the *kind* of thing
  (`tool_call`, `data_access`, `agent_handoff`, `llm_call`, `approval`).
  `action` is the *verb* (`read`, `write`, `delete`, `invoke`, `delegate`,
  `approve`). Governance cares about the verb independently of the mechanism —
  a write is a write whether it arrived via MCP, a notebook, or a stored proc.
- **`resource` vs `data_domain`.** `resource` is the concrete thing
  (`SALES.PUBLIC.FCT_ORDERS`, `https://api.weather.gov/...`). `data_domain` is
  the governance classification (`sales`, `hr`, `inventory`). Policies are
  written against domains because concrete resource names change constantly and
  differ per platform.

`status` includes **`denied`** as distinct from `failure`. A gateway refusing an
action is a governance signal; a 500 is an infrastructure signal. Collapsing
them loses the thing we most want to alert on.

### Chain reconstruction

Three fields carry the graph:

- `correlation_id` — groups every event in one logical execution (the run)
- `parent_event_id` — the edge in the call tree
- `actor_id` / `actor_type` — the principal for this step

That is enough for both required shapes:

```
Human (actor_type=human)
  └─ Forecast Agent          agent_run_start, parent=None
       └─ Weather MCP Tool   tool_call,   parent=run_start
            └─ Weather API   data_access, parent=tool_call

Agent A
  └─ Agent B                 agent_handoff (action=delegate) then
                             agent_run_start with actor_id=<Agent A>
       └─ MCP Tool           tool_call
            └─ Snowflake     data_access, platform=snowflake
```

`ExecutionChain.walk()` returns `(depth, event)` depth-first. It treats an event
whose parent is outside the chain as a root (so a truncated export still
renders) and tracks visited ids (so a data bug produces a flat list, not an
infinite loop).

### Registries: `Agent` and `Tool`

Events are what *happened*. Registries are what was *supposed* to happen.
Findings are the difference.

- `config/agents.yaml` — inventory plus each agent's governance envelope
- `config/tools.yaml` — inventory plus `is_mutating`, `requires_approval`
- `config/data_domains.yaml` — the data-domain vocabulary plus mapping rules

These live in version control, not the database, so a governance change is a
reviewable diff with an author and a date. The tool registry matters
independently: it lets the read-only rule fire on a tool's *real capability*
rather than on whatever the calling agent claimed it was doing.

## Tenancy

**Decision: `tenant_id` is required everywhere, and every read takes it as a
positional argument.**

It sits on `AgentEvent`, `PolicyResult`, `ExecutionChain` and `PolicyReport`, and
on every registry entry. There is no default on the model — an event that does
not know whose it is cannot be stored. `Settings.tenant_id` supplies the value at
the composition root (`ACP_TENANT_ID`, default `"default"`); single-tenant
deployments still carry one, because the point is that the schema never assumes
only one organisation can exist.

Four properties follow:

1. **Reads cannot forget the filter.** `list_events(tenant_id, ...)` takes it
   positionally rather than as an optional keyword. A forgettable tenant filter
   is one someone will forget, and cross-tenant bleed is silent when it happens.
   `list_tenants()` is the single deliberate exception, for admin tooling.
2. **Writes take tenancy from the data.** `save_events()` reads `tenant_id` off
   each event, so a caller cannot file one tenant's data under another's label.
   Storage is keyed on `(tenant_id, event_id)`, so the same id in two tenants is
   two rows.
3. **Chains never mix tenants.** `build_chain` rejects a mixed batch outright
   rather than picking a winner, and `AgentEvent.fingerprint` includes the tenant
   so deduplication cannot merge across the boundary.
4. **One `ControlPlane` serves one tenant.** Serving several means building
   several. That makes cross-tenant access an explicit act rather than a
   forgotten `WHERE` clause.

Registry entries with no explicit `tenant_id` belong to the default tenant, and
`load_registry` filters to the requested one. A second tenant therefore starts
with an empty inventory and an empty vocabulary rather than silently inheriting
the first tenant's governance.

## Data domains: vendor object → controlled term

**Decision: a data domain is a governance term we declare, never a string a
vendor logged.**

`data_domain` was a free string in Tollgate 1. That meant a typo (`inventry`)
silently became an unauthorized-domain violation, and any gateway could invent a
classification the policy engine would then trust. The pipeline is now explicit:

```
vendor object  ->  mapping rule  ->  controlled domain
```

`config/data_domains.yaml` declares both halves. Domains carry a `sensitivity`
(`public` / `internal` / `confidential` / `restricted`) and an owner; mappings
are case-insensitive globs over each platform's natural identifier —
`SALES*.*.*` for a Snowflake `db.schema.object`, `ERP://SHIPMENTS/*` for a URI.

- **Most specific wins**: fewest wildcards, then longest pattern. Adding a narrow
  rule never requires reordering the file by hand.
- **Mappings are platform-scoped**, because the same identifier means different
  things on different platforms.
- **The vendor's `data_domain` is a claim**, not an answer. It survives only if
  it is in the vocabulary, and a matching mapping rule always beats it.
- **A mapping onto an undeclared domain is a load-time error.** Otherwise it
  surfaces much later as a mysterious `unknown`.

When nothing matches, the answer is the reserved `unknown` domain — never the
vendor's string, and never `None`. `unknown` is intentionally *not* declarable in
the vocabulary, so it cannot be granted to anyone.

Who gets blamed for it matters. An unclassified object is *our* gap, not the
agent's, so `unknown_data_domain` is medium and decides `allow`, and
`unauthorized_data_domain` skips `unknown` entirely. Without that split, every
mapping we hadn't written yet became a HIGH would-block finding against an agent
for touching a domain nobody could have been allowlisted for.

Adapters get the vocabulary injected (`MCPCallLogSource(..., domains=...)`). With
none configured the adapter passes claims through unchanged, so it stays usable
standalone — it just doesn't invent classifications when a vocabulary *does*
exist.

## Access scope: why an empty list means nothing

**Decision: an empty collection never carries security meaning.**

Access scope is always two fields — a mode and a list:

```yaml
tool_access_mode: unrestricted      # everything allowed; a list here is rejected
# --- or ---
tool_access_mode: allowlist
allowed_tools: [sales.query_history, forecast.run_model]
# --- or ---
tool_access_mode: allowlist
allowed_tools: []                   # deny all
```

The rejected alternative was "empty allowlist means unrestricted". It is
convenient and it is a latent security bug: someone removes the last entry from
an allowlist during a cleanup and the agent silently gains access to everything.
The failure is invisible in review — the diff shows a line being deleted, not a
policy being inverted.

Three properties fall out, all enforced in `models/agent.py`:

1. `mode` is **mandatory**. A bare list without a mode is a validation error.
2. `unrestricted` **plus a non-empty list is a validation error**, not a
   silently-ignored list. A list that looks like a restriction but isn't
   enforced is worse than no list.
3. `allowlist` + `[]` is **deny-all**, and says so in the UI
   (`allowlist (empty: deny all)`).

Rules never ask "is this collection empty?". They ask `scope.permits(name)`.

`AgentGovernance` defaults to `unrestricted` scopes with `access_mode:
read_only`. That default is explicit and visible rather than implied by
emptiness. It applies to unregistered (shadow) agents, which are separately
flagged by `unregistered_agent` — we don't claim an agent violated an allowlist
nobody ever wrote, but we do record that it exists ungoverned.

## Adapter pattern

```python
class EventSource(Protocol):
    @property
    def name(self) -> str: ...
    def fetch(self, since: datetime | None = None) -> Iterable[AgentEvent]: ...
```

That is the entire contract. An adapter owns auth, pagination, vendor field
names and cost arithmetic; it hands back normalized events and nothing else. It
must not touch the repository, the policy engine or the dashboard.

`normalize_all()` in `adapters/base.py` isolates per-record failures: one
malformed row in an audit export is logged and skipped, not fatal.
`strict=True` flips that for tests and for sources where silent loss is
unacceptable.

Shipped adapters:

- **`synthetic.py`** — the Weekly Sales Forecast scenario. Deterministic for a
  given seed (uuid5 ids, seeded `Random`), so tests and demos are stable.
- **`mcp.py`** — reads a JSONL call log of the shape an MCP gateway emits. It
  deliberately does **not** import an MCP SDK: the control plane consumes logs,
  not live protocol traffic, which keeps it testable offline and usable against
  any gateway that can write JSONL.

One mapping decision worth calling out: an MCP `tools/call` with no declared
`op` maps to `action=invoke`, **not** `read`. Defaulting to `read` would
understate risk; staying neutral lets the tool registry decide whether the call
mutates anything.

Unmapped MCP methods raise rather than guess. Guessing at the adapter boundary
corrupts every downstream governance answer.

## Ingestion flow

```
source.fetch(since)
   → stamp ingested_at
   → repository.save_events()        INSERT OR IGNORE on event_id (idempotent)
   → for each correlation_id touched:
        repository.get_chain()
        engine.evaluate(chain, ctx)  → PolicyReport
        repository.delete_policy_results(run)   ← results are derived
        repository.save_policy_results()
```

Two properties:

- **Ingestion is idempotent.** Re-running an adapter writes zero rows. Adapters
  can therefore over-fetch on overlapping windows without dedup logic of their
  own.
- **Policy results are derived, never source of truth.** They are deleted and
  rewritten per run on every evaluation, so a rule change never leaves stale
  findings behind. `agent_control_plane evaluate` re-runs everything.

## Persistence

SQLite via stdlib `sqlite3`. Two tables — `events`, `policy_results` — plus
`schema_meta` holding a schema version, and indexes on `correlation_id`,
`agent_id`, `timestamp`, `parent_event_id`. Both tables are keyed on
`(tenant_id, …)` and every index leads with `tenant_id`, so a tenant-scoped read
is the cheap path rather than a filter applied after the fact.

Chosen because it ships with Python, is transactional, needs no service, and the
MVP's query volume is trivial. The cost is that column-scan analytics over
millions of events will be slow.

That cost is contained: `ControlPlaneRepository` is the seam. A DuckDB
implementation is a new class behind the same Protocol, and `bootstrap.py` is
the only file that changes. Migration path when volume demands it:

1. Implement `DuckDBRepository` against the same Protocol.
2. Push the analytics aggregates down into SQL in that implementation.
3. Keep SQLite for local dev.

Analytics currently aggregate **in Python** over the repository Protocol rather
than in SQL. That keeps the read layer store-agnostic and free at MVP scale; it
is the first thing to revisit when the event table stops fitting comfortably in
memory. See `QUESTIONS_FOR_PAUL.md`.

## Policy evaluation

```python
class PolicyRule(Protocol):
    name: str
    severity: Severity
    description: str
    decision: Decision      # allow | deny | require_approval
    def evaluate(self, chain: ExecutionChain, ctx: PolicyContext) -> Iterable[Finding]: ...
```

Adding a rule is: write a class, add it to `default_rules()`. No decorators, no
plugin loader, no registry — the rule list *is* the configuration. Rules receive
everything they may consult via `PolicyContext` (the registry, default
governance, default ceilings, a pinnable `now`), so nothing reads global state
and evaluation is deterministic and unit-testable.

The engine decorates each `Finding` into a `PolicyResult` with a deterministic
`result_id = uuid5(policy, correlation_id, discriminator)`. Re-evaluating a run
therefore *replaces* results instead of accumulating duplicates.

Four behavioural decisions:

**Every rule reports on every run.** A rule that finds nothing emits one PASS
row. This costs a handful of rows and buys the ability to distinguish "no
violations" from "nobody checked" — which is the difference between an audit
artefact and a screenshot.

**A finding is a problem, not an occurrence.** An agent that retries a forbidden
tool eight times has one governance problem. Scope rules group by
`(agent_id, subject)` within a run and put `occurrences`, `event_ids`,
`first_seen`, `last_seen` and a status breakdown into the evidence. Discovered
by running the thing: before grouping, the demo's violation run showed 23
findings, 16 of which were one retry storm drowning out a critical. It now shows
9, with no evidence lost.

**A denied attempt is still a violation.** If the gateway blocks a read-only
agent's write, the attempt is recorded as `critical`. The gateway working is not
the same as the agent behaving, and the next gateway might be misconfigured.

**Decisions are recorded; nothing is enforced.** See below.

### Observe-only, and saying so precisely

This system watches. It does not sit in the request path, and it makes no
latency or availability promise that would let it. But "we only observe" and "we
have no opinion" are different statements, so rules carry a `decision` —
`allow`, `deny` or `require_approval` — describing what a gateway *should* have
done.

That gives `PolicyResult.would_block`: true when a result is a violation **and**
its decision is blocking. The seed run reports `violations=9 would_block=9` on
the bad run and `violations=1 would_block=0` on the shadow-agent run — the
difference between "nine things a gateway would have stopped" and "one thing you
should know about".

Two guards keep this honest:

- A **passing** result cannot carry a blocking decision. There is nothing to
  block.
- `PolicyResult.enforced` exists for the day enforcement lands and is **rejected
  by a validator if set to True today**. An audit trail claiming something was
  blocked when nothing was is worse than having no audit trail.

Turning this into real enforcement means a pre-flight `check(event) -> decision`
entry point an MCP gateway calls synchronously. The rules are ready for it; the
operational commitments around it are a separate product decision (see
`QUESTIONS_FOR_PAUL.md`, Q6).

### Shipped rules

| Rule | Severity | Decision | Grain |
|---|---|---|---|
| `read_only_violation` | critical | deny | per (agent, tool/resource) |
| `unauthorized_data_domain` | high | deny | per (agent, domain) |
| `tool_not_allowlisted` | high | deny | per (agent, tool) |
| `missing_human_approval` | high | require approval | per (agent, subject) |
| `cost_threshold_exceeded` | medium | deny | per (agent, run) |
| `excessive_tool_calls` | medium | deny | per (agent, run) |
| `unregistered_agent` | medium | allow | per (agent, run) |
| `unknown_data_domain` | medium | allow | per (agent, domain) |

`read_only_violation` uses two independent signals — the declared `action` and
the registry's `is_mutating` — and reports which fired in `detected_by`. An
agent's self-reported intent is not evidence.

`missing_human_approval` requires the approval event to *name what it approves*
(`approves_event_id` or `approves_tool` in metadata). "Someone approved
something earlier in the run" is not an audit trail.

`unknown_data_domain` reports two different problems under one name, separated in
the evidence by `reason`: `no_mapping_rule` (we never wrote a mapping for this
object) and `not_in_vocabulary` (someone used a domain name nobody declared — a
typo, or drift between config files). The remediation differs, so the evidence
says which.

Cost and volume ceilings are **per agent within a run**, not per run. When two
agents share a chain, attribution beats a single total.

An LLM-based evaluator, if we ever want one, implements the same Protocol and
sits alongside these. Deterministic rules stay deterministic.

## Future platform integrations

Each is a new module in `adapters/` implementing `EventSource`. No change to
`models/`, `services/` or `repositories/` is anticipated. Sketch of the mapping
work:

### Snowflake

Source: `SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY`, `ACCESS_HISTORY`, and the
Cortex Agent / Snowflake Intelligence request logs.

| Snowflake | `AgentEvent` |
|---|---|
| `QUERY_ID` | `event_id` |
| `USER_NAME` / role | `actor_id`, `actor_type` |
| agent or app identity | `agent_id` |
| `ACCESS_HISTORY.objects_accessed` | `resource`, `data_domain` (via a mapping table) |
| `CREDITS_USED_CLOUD_SERVICES` × rate | `estimated_cost_usd` |
| `EXECUTION_STATUS` | `status` |
| `QUERY_TAG` / session | `correlation_id` |

The `db.schema.object` → domain mapping already exists: add `platform:
snowflake` patterns to `config/data_domains.yaml` and call
`registry.classify(Platform.SNOWFLAKE, resource)` from the adapter. Remaining
open work is correlation, which depends on the agent stamping `QUERY_TAG` —
configuration, not a model change.

### Databricks

Source: system tables (`system.access.audit`, `system.billing.usage`), MLflow
traces, and Unity Catalog / AI Gateway request logs.

MLflow trace spans map almost directly onto the chain fields — span id →
`event_id`, parent span → `parent_event_id`, trace id → `correlation_id`. Unity
Catalog lineage supplies `resource`; billing usage supplies cost. The main work
is joining gateway request logs to the trace so tool calls and LLM calls land in
one chain.

### Microsoft Foundry / Azure

Source: Azure AI Foundry agent run/thread APIs plus diagnostic settings shipped
to Log Analytics.

Thread → `session_id`, run → `correlation_id`, run step → `event_id` with
`parent_event_id` from the step hierarchy. Entra ID identity → `actor_id`.
Token usage per step → cost. Content filter / policy blocks map to
`status=denied`, which is exactly the distinction the model already carries.

### What would force a core change

Being honest about limits — these would touch `models/`, not just adapters:

- **Streaming ingestion.** The model is fine; `EventSource` is pull-shaped.
  A push collector needs a second Protocol (`ingest_events()` already exists as
  the seam).
- **Cross-run causality.** Today a chain is one `correlation_id`. Linking a run
  to the run that scheduled it needs a `parent_correlation_id`.
- **Time-window rules.** The engine evaluates one chain at a time, so a daily or
  weekly budget ceiling has nothing to read across runs. That needs an entry
  point taking a time range rather than a chain.
- **Real enforcement.** Not a model change, but a deployment-shape change: a
  synchronous `check()` in the request path brings latency and availability
  commitments this design does not currently make.

Multi-tenancy *was* on this list at Tollgate 1 and has since landed — see
**Tenancy** above. It cost one required column while the tables were empty;
retrofitting it after real data existed would have been a migration.

None of these are needed for the current scope; all are cheaper to add than to
speculatively design for now.

## Observability of the control plane itself

`agent_control_plane/logging.py` — stdlib logging, key=value by default, JSON when
`ACP_LOG_JSON=1`. Context goes through `log_context(**kwargs)` so ingestion
and policy logs stay greppable and machine-parseable without a logging
framework.

## Testing strategy

No cloud credentials, no network, deterministic throughout.

| Area | Approach |
|---|---|
| Model validation | Invalid events must be *impossible*: naive timestamps, self-parenting, failures with no error, unknown fields |
| Access scope | Its own module — the one place a silent default would be a security bug |
| Tenancy | Mixed-tenant chains rejected, fingerprints differ across tenants, reads never cross the boundary, a second tenant boots against an empty registry |
| Data domains | Most-specific-wins, platform scoping, claims that don't survive the vocabulary, and load-time rejection of orphan mappings |
| Decisions | `would_block` needs both a violation and a blocking decision; `enforced=True` stays impossible while observe-only |
| Persistence | Round-trip every field, idempotency, filters, ordering, re-open without data loss |
| Policy rules | Each rule fires *and* passes; grouping; deny-all; a broken rule doesn't void the evaluation |
| Chain reconstruction | Both required shapes, plus orphans and cycles |
| Adapters | Determinism, chain integrity, the scenario's intended story, malformed-record isolation |
| End-to-end | Ingest → persist → evaluate → analytics against the **committed** `config/`, so a typo in governance config fails the build |
