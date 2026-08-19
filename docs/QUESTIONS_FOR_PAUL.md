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

## Resolved during Tollgate 2

These were open questions above; the code now answers them. Kept here rather
than deleted so the reasoning survives.

### Q8 (resolved). `tenant_id` is now on everything
**Decision taken:** added, and made *required* — no model default. It sits on
`AgentEvent`, `PolicyResult`, `ExecutionChain`, `PolicyReport` and every registry
entry. Every repository read takes it as a positional argument (`list_tenants()`
is the one deliberate exception, for admin tooling); both tables are keyed on
`(tenant_id, …)` and every index leads with it.

**Why now rather than later:** it cost one column while the tables were empty.
The recommendation in the original question was "add it now *if* multi-tenant is
on the roadmap at all", and the cost of being wrong in this direction is a spare
column, while the cost of being wrong in the other is a migration over real audit
data.

**What it buys beyond storage:** `build_chain` rejects mixed-tenant batches
outright, and `fingerprint` includes the tenant, so deduplication cannot merge
across the boundary. One `ControlPlane` serves one tenant; serving several means
building several.

**Still your call:** nothing enforces that the *caller* is entitled to the tenant
it asks for. That is an authn/authz concern and there is no auth layer here yet.
Today's guarantee is "no accidental bleed", not "no malicious access". 🟡

### Q10 (resolved). `data_domain` is a controlled vocabulary
**Decision taken:** the `data_domains.yaml` registry the original question
recommended, plus the `unknown_data_domain` rule. Domains carry a `sensitivity`
and an owner; glob mapping rules turn vendor objects into governance terms, most
specific wins, and a mapping onto an undeclared domain fails at load.

**One thing that fell out of building it:** the vendor's own `data_domain` field
had to become a *claim* rather than an answer, otherwise any gateway could invent
a classification the policy engine would then trust. It now survives only if it
is in the vocabulary, and a mapping rule beats it.

**And one bug the demo surfaced:** with `unknown` treated as an ordinary domain,
`unauthorized_data_domain` fired on it — a HIGH would-block finding against an
agent for touching a domain nobody could have been allowlisted for. It now skips
`unknown` and lets `unknown_data_domain` (medium, allow) own that case. An
unclassified object is our gap, not theirs.

**Still your call:** `sensitivity` is declared but no rule reads it yet. The
obvious use is a rule like "restricted data requires an approval regardless of
allowlist". Cheap to add once you say what the ladder should mean. 🟢

### Q21 / Q22 (resolved). Package and naming
**Decision taken:** `agentops` → `agent_control_plane`, console script
`agentops` → `acp`, env prefix `AGENTOPS_` → `ACP_`. Git tracked it as renames,
so history survived.

This settles the PyPI collision Q21 raised. Q22's directory-name mismatch is
unchanged and remains cosmetic — the folder is still `agent control plane` with
spaces, which only makes shell quoting fiddly on Windows. Rename it whenever it
starts to annoy; nothing depends on it. 🟢

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

**Note:** `would_block` (added for Q6) is *not* this flag and shouldn't be
mistaken for it. It means "a gateway should have stopped this"; you are asking
for "a gateway did stop this". The second is derivable from the evidence today —
findings carry a `statuses` breakdown, so `{"denied": 1}` is the signal — it just
isn't surfaced as a first-class field.

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

**Update (Tollgate 2):** still observe-only, but rules now *record* what a
gateway should have done. Each rule carries a `decision` (`allow` / `deny` /
`require_approval`) and each violating result exposes `would_block`. The seed run
reports `violations=9 would_block=9` on the bad run versus `violations=1
would_block=0` on the shadow-agent run.

`PolicyResult.enforced` exists and is rejected by a validator if ever set True —
an audit trail that claims something was blocked when nothing was is worse than
no audit trail. Flipping that guard is the moment this becomes a different
product, and it should be a deliberate decision, not a config change.

**Recommendation unchanged:** stay observe-only. But the `would_block` numbers
are now the cheapest way to find out what enforcement *would* have cost you
before committing to it — worth looking at before Tollgate 3. **Cost:** 🔴 not
the rules, but the operational commitments around them.

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
→ **Resolved in Tollgate 2.** See the resolved section above.

### Q9. Cost is a number with no currency or pricing provenance
`estimated_cost_usd` is a float. No record of which price list produced it, and
no way to re-cost historical events when rates change.

**Recommendation:** fine for MVP. If cost becomes a chargeback input rather than
a dashboard number, add `pricing_version` and keep raw units (credits, tokens)
alongside. **Cost:** 🟡

### Q10. `data_domain` is a free string
→ **Resolved in Tollgate 2.** See the resolved section above.

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

### Q18. `ruff format` is not enforced — **still yours to decide**
`ruff check` passes clean over `src`, `tests`, `scripts`. `ruff format --check`
now reports **19 files would be reformatted, 19 already formatted** — so the repo
is currently in exactly the half-formatted state this question warned about, and
the split is getting worse, not better.

I did not run it. You asked me to move Q8/Q10/Q21/Q22 to resolved and this was
not on that list, and reformatting 19 files on top of a rename-heavy diff would
make the substantive changes much harder to review.

**Recommendation unchanged, and now overdue:** adopt it wholesale in one isolated
mechanical commit (`ruff format src tests scripts`, tests confirm nothing moved),
or delete the aspiration. Either is fine. The current state is the one option
that is not. **Cost:** 🟢 now, 🟡 later.

### Q19. No CI
Tests, lint and a seed run are all one command each. There's no GitHub Actions
workflow.

**Recommendation:** add one in Tollgate 2 — it's ~20 lines and this repo is
already credential-free and deterministic, which is exactly what makes CI easy.
**Cost:** 🟢

### Q20. `mypy --strict` — now clean, but only over `src`
**Update (Tollgate 2):** green. `Success: no issues found in 26 source files`.
It caught one real defect on the way: `cost_by_data_domain` accumulated floats
into a `Counter[str]`, which is int-typed.

**Still open:** the config sets `packages = ["agent_control_plane"]`, so `tests/`
and `scripts/` are unchecked. Test code is where a wrong type is most likely to
mean a test that passes vacuously.

**Recommendation:** extend the check to `tests` when you add CI (Q19), not
before — it will surface a batch of fixture annotations and that is better
handled in one pass. **Cost:** 🟢

---

## 5. Packaging

### Q21. The package was named `agentops`
→ **Resolved in Tollgate 2.** Renamed to `agent_control_plane`; see above.

### Q22. Repo/directory naming mismatch
→ **Resolved in Tollgate 2** as far as it matters. The directory still has
spaces in it, which is cosmetic; see above.

### Q23. Parent directory is itself a git repo
`C:/Users/paulc/Documents` is a git repository containing ~20 untracked
projects. I left it completely untouched per your instruction. This project now
has its own repo nested inside it, which git will treat as an embedded
repository if anyone ever stages it from the parent.

**Recommendation:** clean up the parent separately, as you planned. Until then,
avoid running `git add` from `Documents`. **Cost:** 🟢 if handled deliberately.
