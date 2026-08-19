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

### Q18. `ruff format` — **adopted**
→ **Resolved.** Applied wholesale in one mechanical commit (`ruff format .`),
which is the option this question recommended. The repo had drifted to 20
unformatted files against 21 formatted — precisely the half-formatted state
flagged as the one outcome worse than either choice.

Verified mechanical rather than assumed: every changed `.py` file was parsed
before and after and the ASTs compared — 19 files changed, 0 AST differences.
The 20th changed file is `docs/ARCHITECTURE.md`, where ruff 0.16 formats the
Python code block in the `PolicyRule` snippet.

`ruff format --check .` is now a CI gate, so it cannot drift again. 🟢

### Q19. No CI
→ **Resolved.** `.github/workflows/ci.yml`: one job on pull requests and pushes
to `main`, running format, lint, `mypy --strict`, `pytest --cov`, the seed
demo, and the five CLI commands.

**One decision worth your attention:** no matrix, and pinned to **3.12**. Every
tool config in the repo already says 3.12 (`requires-python`, ruff
`target-version`, mypy `python_version`), so that is what the project claims to
support. But it is developed on 3.13, which CI therefore never exercises. If
3.13 is the real deployment target, either say so in `requires-python` or add
the second interpreter — right now the declared floor and the actual dev
environment disagree, and CI follows the declaration. 🟢

**Coverage:** configured in `pyproject.toml`, not in the CI command, so the gate
is identical locally and in CI. `fail_under = 96` sits just under the current
96.87% — tight enough to catch a real regression, loose enough not to fail on a
fractional shift. Deliberately *not* in `pytest addopts`: a plain `pytest` stays
fast and ungated.

### Q20. `mypy --strict` scope
→ **Resolved.** Now covers `src`, `tests` and `scripts` — 38 files, clean, with
no new `type: ignore` comments and no production code changed to satisfy it.

Extending it found six things, two of which were real rather than cosmetic:

- The `repo` and `control_plane` yield-fixtures in `conftest.py` were annotated
  with the yielded type instead of `Iterator[...]`. Simply wrong as written.
- The `Exploding` test double stopped satisfying `PolicyRule` when the protocol
  gained `decision`, so the "a broken rule does not void the evaluation" test
  was asserting engine behaviour about something that is not a rule. It now
  carries a decision and is bound to `PolicyRule` so the protocol is checked
  rather than assumed.

The rest were a bare `list` annotation, a `platform="local"` string where the
enum is declared, and one `type: ignore` mypy now reports as unused.

**Earlier note, now settled:** the concern was that this would need a batch of
fixture churn. It did not — six errors, all fixed at the site. 🟢

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
