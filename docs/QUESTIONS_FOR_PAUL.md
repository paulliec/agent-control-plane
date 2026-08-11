# Questions for Paul

Decisions taken with a default so work could continue. Nothing here blocked
Tollgate 1. Each entry: the question, why it matters, what I did, the
alternatives, and what it costs to change later.

**Cost-to-change key:** 🟢 cheap (an afternoon) · 🟡 moderate (migration or
re-evaluation) · 🔴 expensive (changes the meaning of stored data)

---

## Resolved during Tollgate 1

### Q0. Empty-allowlist semantics — **you decided this**
Empty collections no longer carry security meaning. Scope is `mode` + `list`;
`allowlist` + `[]` is deny-all; `unrestricted` + a list is a validation error.
Implemented in `models/agent.py`, documented in `ARCHITECTURE.md`, tested in
`tests/test_access_scope.py`. Nothing further needed.

---

## 1. Product / governance

### Q1. What is a "violation" — one per event, or one per problem?
**Why it matters:** it changes every headline number and every alert volume.

**What I did:** one finding per `(agent, subject)` per run, with `occurrences`,
`event_ids`, `first_seen`/`last_seen` and a status breakdown in the evidence.
Discovered by running the demo: per-event findings turned one retry storm into
16 of 23 findings in a run, burying the critical one. Grouping took it to 9 with
no evidence lost.

**Alternatives:** per-event (maximum audit fidelity, unusable signal-to-noise);
per-rule-per-run (too coarse — two different forbidden tools become one row).

**Cost to change:** 🟢 evidence retains every event id either way.

### Q2. Should a *blocked* attempt count as a violation?
**Why it matters:** it's the difference between "we had 0 incidents" and "we had
14 attempts our gateway happened to catch".

**What I did:** yes. `status=denied` still produces a finding. The demo's
`inventory.adjust_stock` attempt is `critical` despite the gateway rejecting it.

**Alternatives:** treat denied as informational; or a separate lower severity
for "attempted but blocked".

**Recommendation:** keep as-is, but consider a `blocked` flag on the finding so
a dashboard can show "attempted vs succeeded" separately. **Cost:** 🟢

### Q3. What governance applies to an unregistered (shadow) agent?
**Why it matters:** it decides whether shadow agents produce one finding or
dozens.

**What I did:** default governance — `read_only`, `unrestricted` scopes,
`$1.00` / 25 tool calls — plus the `unregistered_agent` finding. We don't claim
an agent violated an allowlist nobody wrote, but we do record it exists.

**Alternatives:** deny-all defaults (every shadow event becomes 2–3 extra
findings, very noisy); no default at all (shadow agents produce nothing but the
inventory finding).

**Recommendation:** revisit once you have real shadow-agent volume. Configurable
today via `PolicyContext.default_governance`. **Cost:** 🟢

### Q4. Are cost/volume ceilings per run, per agent-per-run, or per day?
**What I did:** per agent per run. When two agents share a chain, attribution
beats one total.

**Not covered:** an agent that runs 500 cheap runs a day stays under every
ceiling. Daily/weekly budget rules need a time-window rule that reads across
runs — the engine currently evaluates one chain at a time.

**Recommendation:** add a `WindowRule` variant in Tollgate 2/3 if budget
enforcement matters. **Cost:** 🟡 needs an engine entry point that takes a time
range rather than a chain.

### Q5. Are the demo ceilings realistic?
I set the forecast agent to `$2.50` / 12 tool calls. The 12 came from
observation: healthy runs use 5–6, so the original 20 would never have fired.
**A ceiling far above normal usage is decoration, not a guardrail.** Real
numbers should come from your actual telemetry. **Cost:** 🟢 config only.

### Q6. Should the control plane ever *enforce*, or only observe?
Everything here is after-the-fact. The obvious next step — a pre-flight
`check(event) -> allow/deny` an MCP gateway could call — is a materially
different product with latency and availability requirements.

**Recommendation:** stay observe-only through Tollgate 2. Flag it if enforcement
is the actual goal, because it changes deployment shape. **Cost:** 🔴 not the
rules, but the operational commitments around them.

---

## 2. Data model

### Q7. Closed enums for `platform`, `event_type`, `action`, `status`
**What I did:** closed. An unmapped vendor value is a loud adapter error, not a
silent free-text row. `extra="forbid"` on `AgentEvent` likewise.

**Trade-off:** every new platform needs a one-line enum addition. That's the
point — it forces a conscious mapping decision.

**Cost to change:** 🟢 loosening later is trivial; tightening after free-text
data exists is 🔴. Staying closed now keeps the cheap option open.

### Q8. No `tenant_id` on `AgentEvent`
Single-tenant assumed. Multi-tenant later means a migration and a filter on
every query.

**Recommendation:** add it now *if* multi-tenant is on the roadmap at all — it
costs one nullable column today. **Cost:** 🟡 now, 🔴 after real data.

### Q9. Cost is a number with no currency or pricing provenance
`estimated_cost_usd` is a float. No record of which price list produced it, and
no way to re-cost historical events when rates change.

**Recommendation:** fine for MVP. If cost becomes a chargeback input rather than
a dashboard number, add `pricing_version` and keep raw units (credits, tokens)
alongside. **Cost:** 🟡

### Q10. `data_domain` is a free string
Deliberately not an enum — domains are customer-specific taxonomy. But nothing
validates that an event's domain exists in any registry, so a typo
(`inventry`) silently becomes an unauthorized-domain violation.

**Recommendation:** add a `data_domains.yaml` registry and a `unknown_data_domain`
rule. Cheap and catches real config drift. **Cost:** 🟢

### Q11. Cross-run causality
A chain is one `correlation_id`. If run B is scheduled by run A, nothing links
them. Needs `parent_correlation_id`.

**Recommendation:** defer until a real scenario needs it. **Cost:** 🟡

---

## 3. Storage and scale

### Q12. SQLite vs DuckDB
**What I did:** SQLite (stdlib, transactional, no service). `ControlPlaneRepository`
is the seam; a DuckDB implementation is a new class and a one-line change in
`bootstrap.py`.

**Trade-off:** column-scan analytics over millions of events will be slow.

**Cost to change:** 🟢 by design.

### Q13. Analytics aggregate in Python, not SQL
`AnalyticsService` loads events through the Protocol and aggregates in memory.
Store-agnostic and free at MVP scale; it will not survive a large table.

**Recommendation:** the first symptom will be a slow dashboard. At that point
push the aggregates into the DuckDB implementation. Keep the typed return
shapes (`Overview`, `RunSummary`, …) so callers don't change. **Cost:** 🟡

### Q14. No retention or archival policy
Events accumulate forever. Nothing prunes, nothing archives.

**Recommendation:** decide retention before this touches real audit data —
it's usually a compliance answer, not an engineering one. **Cost:** 🟢 to add,
🔴 to decide late if data was deleted or kept wrongly.

---

## 4. Operations

### Q15. Registries in YAML files, not the database
Governance config is version-controlled so a change is a reviewable diff with an
author and a date.

**Trade-off:** no UI editing, and no history of *who* changed an allowlist
beyond git.

**Recommendation:** keep for now; git blame is a better audit trail than most
admin UIs. If non-engineers need to edit it, that's a Tollgate 3 conversation.
**Cost:** 🟡

### Q16. Policy results are deleted and rewritten on every evaluation
Derived data, so a rule change never leaves stale findings. But there's no
history: you can't answer "was this flagged last month under the old rules?"

**Recommendation:** if findings ever drive tickets or attestations, add an
append-only `policy_result_history` table with a `ruleset_version`. **Cost:** 🟡

### Q17. Nothing records which version of the rules produced a finding
Related to Q16. A finding is reproducible only against the current code.

**Recommendation:** add `ruleset_version` to `PolicyResult` when rules start
changing regularly. **Cost:** 🟢

### Q18. `ruff format` is not enforced
`ruff check` passes clean and runs on `src`, `tests`, `scripts`.
`ruff format --check` would reformat 13 files — mostly re-wrapping deliberate
alignment. I didn't run it rather than churn the diff on day one.

**Recommendation:** decide now, before the codebase grows: either adopt
`ruff format` wholesale in one commit, or drop it. Half-formatted is the worst
option. **Cost:** 🟢 now, 🟡 later (a large mechanical diff).

### Q19. No CI
Tests, lint and a seed run are all one command each. There's no GitHub Actions
workflow.

**Recommendation:** add one in Tollgate 2 — it's ~20 lines and this repo is
already credential-free and deterministic, which is exactly what makes CI easy.
**Cost:** 🟢

### Q20. `mypy --strict` is configured but not clean
`pyproject.toml` has a strict mypy block; I haven't run it to green. Type hints
are thorough but unverified.

**Recommendation:** either get it clean in Tollgate 2 or relax the config so it
isn't aspirational. **Cost:** 🟢

---

## 5. Packaging

### Q21. The package is named `agentops`
There is an unrelated `agentops` package on PyPI. Fine for a private/internal
project; a problem if this is ever published or if someone pip-installs both.

**Recommendation:** rename to `acp` or `agentcp` **now** if publication is
plausible. Renaming after external consumers exist is painful.
**Cost:** 🟢 today, 🔴 later.

### Q22. Repo/directory naming mismatch
Directory: `agent control plane` (with spaces). Project name:
`agent-control-plane-lite`. Remote: `agent-control-plane`. Nothing breaks, but
the spaces make shell quoting fiddly on Windows.

**Recommendation:** cosmetic; rename the directory to `agent-control-plane` if
it starts to annoy. **Cost:** 🟢

### Q23. Parent directory is itself a git repo
`C:/Users/paulc/Documents` is a git repository containing ~20 untracked
projects. I left it completely untouched per your instruction. This project now
has its own repo nested inside it, which git will treat as an embedded
repository if anyone ever stages it from the parent.

**Recommendation:** clean up the parent separately, as you planned. Until then,
avoid running `git add` from `Documents`. **Cost:** 🟢 if handled deliberately.
