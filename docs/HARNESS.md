# Development harness

How an agent may work on this repo without someone watching, and what stops it
doing anything else. Status: **Phase 0/1.5** (`docs/NEXT.md` item 3), plus the **pilot runner** (item 4),
implemented but never run live. Nothing runs on a schedule, nothing publishes,
nothing opens a PR.

Rules of the road still come from `CLAUDE.md` and `docs/NEXT.md`. This file
covers the machinery around them.

## Pieces

| Piece | File | Native Claude Code feature or ours |
|---|---|---|
| Task contract | `.github/ISSUE_TEMPLATE/agent-task.yml` | GitHub issue form |
| Contract parsing and eligibility | `scripts/harness/contract.py` | ours |
| Scope check (diff against contract and protected paths) | `scripts/harness/scope.py` | ours |
| Trusted verifier (gate) | `scripts/harness/gate.py` → `scripts/verify.sh` | ours |
| Guard hook | `scripts/harness/guard.py`, `guard.sh` | native `PreToolUse` hook, our logic |
| Unattended permissions | `.claude/harness/settings.json` | native permission rules, `dontAsk` mode |
| Implementer / reviewer | `.claude/agents/{implementer,reviewer}.md` | native subagents |
| Repo-specific config | `harness.toml` | ours |
| Sample contract | `docs/harness/pilot-demo-counts.md` | — |

The generic parts are `scripts/harness/`, the two agents, the settings file and
the issue form. Everything ACP-specific sits in `harness.toml`, `CLAUDE.md` and
`docs/NEXT.md`.

## Lifecycle of a task

1. Paul files an issue from the **Agent task** form.
2. Paul applies `acp:approved`. That label is the approval.
3. `contract check-issue N` decides whether the issue is eligible (read-only `gh` query):
   - the issue is open, and both its author and whoever applied the label are `harness.toml` `owner`
   - the most recent event for the approval label is a `labeled` event by the owner
   - the body has not been edited since the label went on (an edit revokes approval; re-label to approve the new text)
   - the issue is not labelled `acp:blocked`
   - the contract parses, its paths don't overlap protected ones, and its criteria use only the allowed vocabulary
   - its "Approved work item" matches an entry under `NEXT.md` "Current approved work" exactly
4. *(Phase 2, not built.)* A runner creates `agent/<n>-<slug>`, invokes the
   implementer headless, runs the gate, gives the implementer at most
   `repair_attempts` more tries, then hands the contract and the diff to the
   reviewer. The reviewer never sees the implementer's summary.
5. The **trusted verifier** (`gate`) judges the candidate branch:
   - **Pinned baseline.** It runs only from a clean checkout whose HEAD is the
     pinned base (a full SHA). Gate code, `harness.toml` and the contract
     (`git show <base>:<path>`) all come from that commit, never from the
     candidate.
   - **Git-only checks.** The candidate is a ref, or a bundle quarantined at
     `refs/harness/candidate`. It must be named `agent/*` and descend from the
     base, and it may contain no merge commits. **Every** commit, not just
     the net diff (`--no-renames`), must stay inside Allowed paths and outside
     `protected`, so a protected edit reverted later or a file added then
     deleted is still caught. None of this checks out or runs candidate code,
     so it is safe on the host.
   - **`--run-criteria`** executes candidate code in a detached worktree. It
     refuses on a Windows host and unless `ACP_SANDBOX=1`:
     - `verify` runs `scripts/verify.sh`
     - `pytest <node>` runs pytest on that node only; a node that collects no tests fails
     - `manual: ...` is listed as pending for the reviewer and Paul

   A task is accepted only with `ok: true` **and** `criteria_run: true`. A
   criteria run is never `ok` unless `verify` ran and passed, even if a
   contract somehow reached the gate without one.
6. *(Phase 3, not built.)* Draft PR against `main`. CI on that PR is the
   authority; the agent never merges.

```
python -m scripts.harness.contract check-file docs/harness/pilot-demo-counts.md
python -m scripts.harness.contract check-issue 12
# host-safe: git checks only
python -m scripts.harness.gate --base <H> --ref agent/1-demo-counts     --bundle agent.bundle --contract docs/harness/pilot-demo-counts.md
# sandbox only: also runs verify.sh and the pytest node
ACP_SANDBOX=1 python -m scripts.harness.gate ... --run-criteria
```

### Acceptance criteria vocabulary

Contracts can't contain commands. A criterion is one of exactly three forms:

| Form | What runs |
|---|---|
| `verify` (required in every contract) | `harness.toml` `[verify] command` |
| `pytest tests/<path>.py[::<name>]` | `python -m pytest <node> -q`; the node must match `^tests/[\w/]+\.py(::\w+)*$` |
| `manual: <text>` | nothing; the reviewer and Paul judge it |

## Running the engine headless

The invocation, as validated against Claude Code **2.1.325**:

```
claude -p \
  --settings .claude/harness/settings.json --setting-sources project \
  --permission-mode dontAsk --permission-prompts none \
  --max-turns 20 --max-budget-usd <validated cap> \
  --no-session-persistence --output-format json \
  --agent implementer "<contract>" < /dev/null
```

- `--setting-sources project` drops user settings, so nothing in
  `~/.claude/settings.json` (an allow rule, `skipDangerousModePermissionPrompt`)
  leaks into the run. The harness settings are added on top via `--settings`.
- `dontAsk` + `--permission-prompts none`: anything not explicitly allowed is
  denied instead of waiting for a person who isn't there.
- `disableBypassPermissionsMode: "disable"` in the settings file. Never pass
  `--dangerously-skip-permissions` or `--allow-dangerously-skip-permissions`.
- `< /dev/null`: without it `-p` waits 3 s for stdin and warns.

### What was checked against the installed CLI (2026-11-15)

Eight live `claude -p` probes, about $0.45 in total. All ran on the host with prompts written by the harness author; no agent-generated code was executed:

| Probe | Result |
|---|---|
| Edit `CLAUDE.md` | denied by permission rule |
| `git push --dry-run` | blocked by the guard hook, so `PreToolUse` hooks run on Windows through Git Bash |
| `git status`, Write `tests/…` | allowed |
| `--max-turns 1` with a three-step prompt | `error_max_turns` after **2** turns. Accepted but missing from `--help`; it counts slightly loosely |
| `--max-budget-usd 0.01` | `error_max_budget_usd` at **$0.035**. The cap is checked between turns, so one turn can overshoot it |
| `--agent reviewer` | its only tools were Read, Grep and Glob; no write was possible |
| After G2: Read, Grep and Glob inside and outside the repo | inside allowed; `..`, `~/.claude` and `~/.ssh` blocked by the guard |
| PR #1 review: a gitignored canary `.env.harness-probe`, at the root and in `tests/` | targeted Grep/Glob (path, `glob`, pattern) blocked by the guard; Read denied by the permission rule. Broad Grep, including `glob: '*'` and `'**/*'`, and Glob `*` never surfaced it: the tools skip hidden files. Glob does list gitignored non-hidden files. The canary value appeared nowhere in the output |

## Budget and accounting

`harness.toml` `[budget]` holds the values approved for P1: **$3.00 per task** in
total, made up of **$2.00 for the implementer** and **$0.75 for the reviewer**,
under a **$5.00 configuration ceiling**. `test_budget_and_limits_are_the_approved_values`
pins them; changing them needs a new approval.

| Limit | Kind | Enforced by |
|---|---|---|
| Workspace spend limit (provider side, set by Paul) | **hard** | Anthropic. The only limit that holds whatever the harness does |
| Per task, cumulative across attempts | refuses up front | `ledger.py`, before every attempt |
| Per invocation ($2.00 implementer, $0.75 reviewer; 20 and 8 turns) | soft | `--max-budget-usd`, `--max-turns`. Checked between turns, so each can overshoot by about one turn (observed: a $0.01 cap stopped at $0.035) |
| One attempt per approval | hard | `ledger.py`: a second reservation after the same approval is refused |
| Wall clock | hard | `timeout-minutes` per job; `timeout_s` on the container |

**Accounting.** Reservations are recorded *before* any model call; the
`account` job settles them afterwards.
1. `authorize` posts a reservation comment for exactly this run attempt
   ($2.75), and reads it back. If it can't see its own reservation, it refuses.
2. The `agent` and `review` jobs start the engine only under that reservation,
   matched by run ID, run attempt, contract digest and base. A GitHub "re-run"
   is a new attempt with no reservation, so it refuses.
3. `account` (`if: always()`) records the costs the engine reported.

A reservation with no account (cancelled, crashed, runner lost) counts at its
full $2.75. A missing, negative or non-numeric cost counts at its allocation.
`account` only ever *lowers* a count to a reported figure; it isn't what makes
accounting durable. Only comments by `github-actions[bot]` count. A malformed
bot record, or comments that can't be read, refuse the run.

**Runtime reconciliation.** `review` recomputes what's left before it pays for
a reviewer: task budget, minus every other attempt as the ledger counts it,
minus this implementer's reported cost (its full $2.00 if unreported). Less
than $0.75 left means no reviewer is run. `verdict` reports final cumulative
spend and refuses readiness on any overrun.

**Overshoot exposure.** With $2.75 reserved and $0.25 of headroom in the task
budget, the worst case per attempt is $2.75 plus about one turn each for the
implementer and the reviewer. After that the ledger refuses further attempts,
and the workspace limit caps everything.

## Controls

**Enforced deterministically** (they don't depend on how the model behaves):
- Permission rules in `dontAsk`: reads only inside the repo; Edit only under `src/` and `tests/`; Bash limited to verify, the project's pytest/ruff/mypy, and local git read/add/commit. Edits to protected paths, `git push`, `gh`, `curl`, `wget`, WebFetch, WebSearch and reading `.env*` are denied.
- The guard hook blocks:
  - writes to protected paths or outside the repo, including on case-insensitive or `..` paths
  - reads and searches (Read, Grep, Glob) outside the repo, and any Read path, Grep path or `glob`, or Glob pattern that names or matches a secrets file (`.env*`, `*.env`)
  - any git use beyond the allowlist, git global options, and `--output`
  - network tools
  - shell commands that push, rewrite history, switch branches, install packages, reach the network, delete recursively or start a nested `claude`
  - commits off an `agent/*` branch

  It fails closed: a crash or a missing interpreter blocks the call.
- The trusted verifier: pinned base, ancestry, `agent/*` ref, and scope on the real commits. It is independent of both layers above and never runs candidate code on the host.
- Contracts can't execute anything, can't grant protected paths, and must include `verify`.
- Eligibility requires the owner as both author and approver, and an unedited body.
- The reviewer has no write or execute tools.
- CI (`.github/workflows/ci.yml`) runs the same gates. The harness changed only its token, and nothing that verifies: `permissions: contents: read` and `persist-credentials: false` (`e322bc4`; a limited scope exception recorded with its approval history in `NEXT.md` item 3, and pinned by a test). No agent can edit it (`.github/*` is protected).

**Not yet guaranteed.** See "Open risks" below.

## Phase 1.5 security fixes

All found in the 2026-11-15 review, all approved, and all fixed. Each was
pinned by a strict `xfail` in `tests/test_harness_adversarial.py` first and is
now a plain passing test with the same assertion or a stronger one. A
mutation check (reverting S1, G5 and the `--output` block) made those tests
fail again.

| Id | Gap | Fix |
|---|---|---|
| G1 | The gate trusted the tree it was judging | The verifier runs only from a clean checkout at the pinned base SHA. Code, config and the contract come from that commit |
| S1 | Rename detection hid the source side of `git mv CLAUDE.md tests/x` | `--no-renames`; only commits are judged |
| G2 | The unscoped `Read` allow rule, and Grep/Glob `path` was unchecked | Changed to `Read(./**)`; the guard checks Grep/Glob `path` and absolute or `..` Glob patterns. Confirmed with a live probe |
| G3 | `conftest.py` was grantable, and it can fake a pass | Protected: `*conftest.py`, `*pytest.ini`, `*tox.ini`, `*setup.cfg`, `*sitecustomize.py`, `*usercustomize.py`, `*.pth` |
| G4 | The guard missed `git -C`, `git -c` and `--output` | Git is parsed with `shlex` and held to an allowlist (status, diff, log, show, add, commit, rev-parse, ls-files). Global options other than `--no-pager` and `-P` are refused, as are `--output` and `commit -n`. Nested `sh -c` and `eval` are blocked |
| G5 | Nothing checked ancestry | The candidate must descend from the base and be named `agent/*`. A bundle must carry exactly that one head |
| R1 (PR #1, P1) | An empty code fence in Acceptance criteria normalised to zero criteria and skipped the verify rule; the gate then accepted a run that verified nothing | The parser requires `verify` unconditionally. The gate never reports a criteria run `ok` without a passing `verify` |
| R2 (PR #1, P2) | Scope looked only at the endpoint diff, so a protected edit reverted later passed | Every commit in `base..tip` is checked, plus the net diff. Merge commits are refused |
| R3 (PR #1, P2) | The `.env` check covered Read only; Grep and Glob returned first | One secret-file check covers Read paths, Grep `path` and `glob`, and Glob patterns. Permissions deny `.env*` and `*.env` at any depth. Confirmed live with a canary |

**Still open:**
- **Secrets in non-dot files.** The search tools skip *hidden* files, so `.env*` is safe even in broad searches. A secret in a non-hidden file, such as `prod.env` or `secrets.yaml`, could still surface through a broad Grep. The guard blocks targeted searches for `*.env`; it can't police a plain `*`. The runner should hold no secrets in the checkout at all.
- **G6.** Allowed tools take paths outside the repo, such as
  `ruff format <host path>` and `pytest --junitxml=<host path>`. Patterns can't
  close this; running in a disposable environment does.
- **Malicious code in an allowed test file** can fake its own result at import
  time. A person reads the diff, and CI reruns independently.

## Supervised vs unattended commits

They are separated by which settings load, not by loosening anything:

- **Supervised** (Paul, or an interactive session Paul is watching) commits on
  `feat/autonomous-harness`. Interactive sessions never load
  `.claude/harness/settings.json`, so neither the guard nor the `agent/*` rule
  applies. That is correct: harness changes are made only on this path.
- **Unattended** runs load the harness settings inside the sandbox, so commits
  are possible only on `agent/*`. The trusted verifier also rejects any bundle
  whose ref is not `agent/*` or does not descend from the pinned base (G5).
  An unattended run can't change the harness, because harness paths are
  protected and the verifier uses the base commit's copy (G1).

## Git baseline

Checked 2026-11-22, read-only:

- **The baseline is published.** The 8 commits `83e1f0b`..`abcb04a` (console
  query layer through the public-demo bootstrap) are on
  `origin/demo-release-readiness`. The harness is on
  `origin/feat/autonomous-harness`, in draft PR #1 against it. `origin/main` and
  `origin/tollgate-2-hardening` are still both `63f332d`.
- **Every local branch is an ancestor of `demo-release-readiness` (`abcb04a`)**:
  `main`, `tollgate-2-hardening`, `tollgate-2-streamlit`. So `main` can later be
  fast-forwarded with nothing lost. `feat/autonomous-harness` sits on `abcb04a`.
- **Integration branch:** `main`, as `CLAUDE.md` and CI already assume. When
  Paul chooses, push `demo-release-readiness` and fast-forward `main` to it. No
  agent does this.
- **Pilot base:** the commit on `feat/autonomous-harness` that contains the
  harness and the approved gap fixes. Call it `H`. Record `git rev-parse H` in
  the run log before bundling. The pilot branch `agent/1-demo-counts` is created
  from `H` by the host, not the agent. Do not use `--base main`: `main...H`
  contains protected files, so every task would fail scope.

## Execution environment

**Goal:** unattended development that keeps running while Paul's PC is off.
That rules out anything that depends on this machine. Windows hooks, the
guard, and `ACP_SANDBOX=1` are configuration. None of them is isolation.

Hosted-option facts come from the Claude Code docs as of 2026-11-15:
`claude-code-on-the-web`, `cloud-environments`, `routines`, `settings` (cloud
sessions), `github-actions`, `headless`, and the claude-code-action security
notes. Anything those pages leave undocumented is marked so here.

| | A. Claude hosted (cloud sessions / routines) | B. GitHub Actions running `claude -p` | C. Local isolated container |
|---|---|---|---|
| Runs with PC off | yes | yes | **no** |
| Compute cost | subscription usage; hourly run caps | free on standard runners for a public repo | free, but this machine |
| Model credential | Anthropic-side; never in the VM | API key secret in the agent job; a dedicated, spend-limited workspace key | API key in the container |
| GitHub credential | held by an Anthropic proxy, outside the VM; **can push any branch** unless rulesets stop it | no `contents: write` anywhere, and checkouts don't keep their token (`persist-credentials: false`); a person publishes | none |
| Our flags (`--settings`, `dontAsk`, `--max-turns`, `--max-budget-usd`) | **not documented**; routines run in auto mode; repo `.claude/settings.json` hooks apply | exact: the same invocation validated locally | exact |
| Network egress | None / Limited allowlist / Full | **unrestricted** on GitHub-hosted runners | fully controllable |
| Independent verifier | not native; needs CI anyway | native: a separate job with no secrets, from the pinned SHA | custom |
| Triggers | cron (at most hourly), API, GitHub events; starts from the default branch | push, schedule, dispatch, issues. Schedule and dispatch need the workflow on the default branch | none hosted |
| Vendor neutrality | Claude-only | orchestration is plain YAML; the engine is one step | neutral |
| Operational work | lowest | one workflow file, one environment, one secret | highest: proxy, images, scheduling |

**Recommendation: B, running the CLI directly, not `claude-code-action`.**
The action's default GitHub App token carries write access, and the docs don't
say whether the job's `permissions:` caps it. Calling `claude -p` directly
keeps the flag set we have already validated. It is the only option where the
agent, the verdict and publishing sit in separate jobs, each holding only what
it needs. The engine is a single step, so another CLI could replace it.

A becomes attractive once Anthropic documents permission-mode, turn and
budget control for routines. Until then it can't enforce `dontAsk`, and it
needs rulesets before its push access is acceptable. C is only for reproducing
a run locally.

### Pilot runner (PR #2, implemented; never run live)

`.github/workflows/agent-pilot.yml`. Each job calls one stage of
`scripts/harness/pilot.py`.

**Trust anchor.** A push to `pilot/<issue>` starts the workflow, but the push
isn't what's trusted. `authorize` requires all of the following:
- the pushed commit is the workflow's own commit (`GITHUB_SHA` == `GITHUB_WORKFLOW_SHA`);
- the checkout is clean at that commit;
- the commit is already on `integration_branch`, so it landed through a reviewed PR;
- the approved issue's **Pinned base** names that commit;
- the issue's contract matches, by digest, the contract committed there.

The digest is taken over the parsed, whitespace-normalised fields, with the
pinned base excluded. Every money-spending job re-checks all of this after its
environment approval. Candidate content exists only as a bundle, and is never
on a branch any workflow runs from.

| Job | Token | Secret | Runs candidate code | Does |
|---|---|---|---|---|
| `authorize` | contents read, issues write | none | no | Trust anchor; ledger check; posts the **reservation** |
| `agent` | contents read, issues read | model key (`acp-agent` approval) | **no**: tools are Read/Edit/Write/Grep/Glob only | The engine edits a separate clone, with a scrubbed environment (no `GH_TOKEN`). Its run must be **complete**: exit 0 and a result reporting `success`. Budget or turn exhaustion, malformed output or an error means `engine_incomplete`, with nothing committed or bundled. Trusted code commits with an external git dir (hooks and fsmonitor off), runs the **pre-upload confidentiality scan**, and only then bundles. Uploads only the sanitised summary, never the raw transcript |
| `check` | contents read | none | no | **Trusted verdict**: pinned base, bundle digest and tip bound, ancestry, no merges, `agent/*`, scope on every commit, secret scan of every added line |
| `criteria` | contents read | none | **yes, in a container** | Wheelhouse built from the base's `pyproject.toml`. Then `trusted_baseline` (base tests), and each `candidate_authored` criterion, each in **its own fresh clone** and container, so no criterion can alter the tree another judges |
| `review` | contents read, issues read | model key (`acp-agent` approval) | no | **Reconciles budget first.** If less than the reviewer's full allocation remains after earlier attempts and the implementer's actual cost (or its full allocation when unreported), no reviewer is run. Otherwise: Read/Grep/Glob only, under `--json-schema`, given the contract, diff and labelled evidence, all digest-checked, never the implementer's transcript. An incomplete reviewer run is never an approval |
| `account` | contents read, issues write | none | no | Settles the reservation with reported costs |
| `verdict` | contents read, issues read | none | no | `ready_for_human` only if every stage passed, both engines completed, every digest matches, and **final cumulative spend** (earlier attempts plus this one, unknown costs at full allocation) is within the task budget. Spend and overrun status are always reported |

**Container** (`sandbox.py`):
- `--network none`;
- an environment built from constants only (no host value is forwarded);
- the runner's non-root uid, with every capability dropped and `no-new-privileges`;
- a read-only root filesystem, with a tmpfs `/tmp` that holds the offline venv;
- 2 CPUs, 6 GB memory, 512 processes, and a 20-minute limit;
- an image pinned by digest;
- only two mounts: the candidate clone and the read-only wheelhouse.

`ACP_SANDBOX=1` is an accident guard only; the container is the isolation.

**Artifact binding.** `check` re-derives the tip and the bundle digest. Every
later job refuses a bundle or evidence file whose sha256 differs from the value
recorded upstream.

**Confidentiality before upload.** Nothing leaves the agent job until trusted
code has checked it:
- Every line the candidate's commits add is scanned for secret-shaped strings
  (including the drill canary) and for the literal value of any credential the
  job holds. A hit means `confidentiality_blocked`: no bundle, and only pattern
  names recorded.
- Engine output is reduced to allow-listed fields, then redacted. The raw
  transcript is deleted.
- Engine stdout and stderr are captured, never streamed to the job log.

`check`'s independent scan of the bundle remains.

**Publication.** None. The output is `agent.bundle` plus evidence. Paul
publishes by hand (see the pilot sequence below).

### What the runner relies on GitHub to enforce

**For P1, credential release is a human decision, not an automated one.** The
harness checks are necessary but not sufficient. Before approving `acp-agent`,
Paul verifies the pinned commit and the workflow it runs.

The trust checks in `pilot.py` run *inside* the workflow, so they protect only a
workflow that calls them. Someone who can push a `pilot/*` branch can push a
modified `agent-pilot.yml` that never runs `authorize` and simply asks for the
`acp-agent` environment. **Nothing in the repository's code can prevent that.**
What prevents it is outside the code:

1. **Only the owner can push** `pilot/*` and `pilot-drill/*`. This repo has one
   writer, and fork PRs can't push to it. Recommended later (a repo setting): a
   ruleset restricting creation of those refs to the owner.
2. **`acp-agent` has a required reviewer**, and deployment branches are limited
   to `pilot/*`. `acp-canary` is limited to `pilot-drill/*`, so a drill branch
   can never reach the real key, whatever its workflow says.
3. **The key exists only as an `acp-agent` environment secret**, never as a
   repository or organization secret. Ordinary CI on PRs runs candidate code,
   and it has no secrets to leak.
4. **Approval checklist.** Approve `acp-agent` only when all three hold:
   - the run's commit is the issue's **Pinned base**;
   - `authorize` has already passed in that same run;
   - the job asking for approval is `agent` or `review`.

   Anything else means the workflow isn't the reviewed one: reject the
   deployment.

An agent can't push at all: no job holds a write token. Its branches reach
GitHub only when Paul pushes a verified bundle, as `agent/<n>`, which this
workflow doesn't trigger on.

### Ledger integrity

- **Concurrent runs.** All pilot runs, real and drill, share one repository-wide
  concurrency group (`acp-pilot`), so they queue. Underneath that, as defence in
  depth: `pilot/7` and `pilot-drill/7` once didn't share a concurrency
  group. If both reserve against one approval, the earliest reservation
  (timestamp, then comment ID) proceeds and the other refuses. Both count as
  spent.
- **Bot identity.** Only comments by `github-actions[bot]` count. No user
  account can hold that login, and the only other workflow here, CI, has no
  `issues` permission.
- **Edited records refuse.** A bot comment whose `updated_at` differs from its
  `created_at` fails the run. Deleted records can't be detected from the
  comments themselves. Only the owner or an admin can delete someone else's
  comment, and the provider spend limit stays the hard cap either way.
- **Run identity and replay.** A reservation belongs to one
  (run ID, run attempt, contract digest, base). Re-runs are new attempts and
  hold no reservation. "Re-run all jobs" reserves again, and that is refused
  under the same approval.
- **Scope of the budget.** The ledger lives on the issue, so the $3 is per
  issue. Opening a new issue for the same contract is a new approval by the
  owner and starts a new budget. The workspace limit caps the total.

### Contract digest

The digest is taken over the parsed fields. Free text has its whitespace
collapsed. List items lose the `- `/`* `/checkbox markers and surrounding
backticks. Pinned base is excluded and checked separately against the commit.

Nothing material is normalised away: case, punctuation, lookalike and
zero-width characters, splitting a token, scope, each criterion, and criterion
order all change the digest. A test pins each of these. Text above the first
`###` heading isn't part of the contract and never reaches a model: both
agents are prompted with the committed file, not the issue.

### Reproducibility

| Pinned | Not pinned |
|---|---|
| Every action, by commit SHA | Python dependencies: `pyproject.toml` declares ranges (`pydantic>=2.7`, `streamlit>=1.35`, …) and there is no lock file, so the wheelhouse resolves whatever is current on PyPI |
| Sandbox image `python:3.12-bookworm@sha256:5f0c…0dd6` | `hatchling`, the build backend |
| Runner image `ubuntu-24.04` | Python patch version (`setup-python` "3.12") |
| `@anthropic-ai/claude-code@2.1.325` | That package's own dependencies and install-time downloads (no lockfile or integrity pin) |
| — | The runner's Docker, git and gh versions |

A constraints file for the wheelhouse is the obvious next step, but it would
change how CI installs, so it's out of scope here.

### Harness test coverage

The suite's coverage figure (97.62%, floor 96%) measures only the
`agent_control_plane` package. Measured separately with
`pytest tests/test_harness*.py --cov=scripts/harness --cov-fail-under=0`,
harness coverage is **85%**:
- `ledger.py`, `scope.py` and `confidential.py`: 100%
- `gate.py` and `guard.py`: 98%
- `config.py`: 92%
- `contract.py`: 80%
- `pilot.py`: 79%
- `sandbox.py`: 71%
- `fake.py`: 0%

What isn't covered can only run against live services, or runs in a
subprocess: the `gh` client and CLI entry points, `docker run` itself, and the
fake engine (exercised by tests, but always in a subprocess coverage doesn't
trace). No threshold changed.

### Properties the runner must enforce, not the harness

The harness code can't guarantee any of these. Each has to come from the
runner configuration or from GitHub:

| Property | Enforced by |
|---|---|
| Untrusted code never runs on Paul's machine | where the job runs: ephemeral GitHub-hosted runners |
| `ACP_SANDBOX=1` is set only where code is isolated | the workflow file, which agents can't edit (`.github/*` is protected) |
| No GitHub write credential where the agent runs | `permissions: {}`, `persist-credentials: false`, no PAT secrets |
| No personal model credential | a dedicated API key in its own Console workspace, never `claude setup-token` |
| Hard spend cap | the workspace spend limit; `--max-budget-usd` overshoots by about one turn |
| Wall-clock and resource limits | `timeout-minutes` and runner size |
| Nothing is pushed automatically | no job has `contents: write` (pinned by a test); Paul pushes the verified bundle by hand. A ruleset comes later (a repo setting) |
| A person approves each run | GitHub environment required reviewers (a repo setting) |
| Network egress limits | **not available** on standard hosted runners. The exposure is accepted and bounded: a spend-limited key and no GitHub token in the job. A third-party egress policy or self-hosted runner is optional later |
| Only the owner can trigger | the workflow's trigger and `if:` on `github.actor`; contract eligibility re-checks the owner |

## First supervised pilot: exact sequence

Paul is present throughout, and each numbered group is a separate approval.

**A. Integrate PR #2.** Push `feat/pilot-runner`, open PR #2 against
`demo-release-readiness`, get an independent review and green CI, then merge.
The merge commit is **H**.

**B. Provider and GitHub setup.**
1. Create an Anthropic Console workspace for this repo with a **$5 monthly
   spend limit**, and an API key in it.
2. Create the GitHub environment `acp-agent`: required reviewer `paulliec`,
   deployment branches `pilot/*`, secret `ANTHROPIC_API_KEY` as an
   **environment** secret only, never a repository or organization secret. See
   "What the runner relies on GitHub to enforce" for the approval checklist.
3. Create the environment `acp-canary` for drills: required reviewer,
   deployment branches `pilot-drill/*`, secret `ANTHROPIC_API_KEY` set to a
   random `ACP_CANARY_...` value. It is never a real key.
4. Create the labels `acp:approved` and `acp:blocked`.

**C. Live drills.** These use the fake engine and cost nothing. They need
scripts under `docs/harness/drills/`, which are added when the drills are
approved. Each drill must end in a verdict that isn't ready, for the stated
reason, and spend no money:
- credential reads from inside the container
- network egress
- protected-path edits
- a canary secret in the diff
- a test that fakes its own pass
- a substituted bundle
- a duplicate push
- an exhausted budget
- an edit to the issue after approval

**D. P1.**
1. Create issue N from the **Agent task** form. Copy the body from
   `docs/harness/pilot-demo-counts.md` and set **Pinned base** = H. Then apply
   `acp:approved`.
2. Push the trigger: `git push origin H:refs/heads/pilot/N`.
3. Approve `acp-agent` for `agent`, and later for `review`, **only** after
   checking against the approval checklist that the run's commit is H and that
   `authorize` passed in that run. Watch the run.
4. Read `verdict`, which shows `ready_for_human`, the problems, and the manual
   criteria, plus final spend and overrun status. Read the evidence artifacts:
   `gate-check.json`, `diff.patch`, `criteria.json`, `review.json`, and the
   sanitised `implementer-summary.json` / `reviewer-summary.json`. The raw
   transcripts are never uploaded.

**E. Human publication.** Only if satisfied:
```
gh run download <run-id> -n agent-1 -D bundle
sha256sum bundle/agent.bundle            # must equal the check job's bundle_sha256
git fetch bundle/agent.bundle agent/N:agent/N
git log --oneline H..agent/N             # exactly the commit the verdict names
git push origin agent/N
gh pr create --draft --base demo-release-readiness --head agent/N
```
CI runs on that PR independently.

**Abort on any of these:**
- a permission denial or guard block nobody expected
- a reservation you didn't expect
- spend above the cap
- any file outside `tests/test_demo_counts.py`

If the documented counts disagree and the implementer stops (`no_changes`),
the harness has passed.

## Publishing

None of this has been done. Each push is Paul's decision.

1. **Check:**
   - Look before pushing:
     `git log -p origin/main..demo-release-readiness` and
     `git diff --stat origin/main..feat/autonomous-harness`.
   - Checked 2026-11-15, the 8 baseline commits had no secrets, binaries,
     databases or `.env` files.
   - The only local path is in `QUESTIONS_FOR_PAUL.md` Q23.
   - The author email is already public in pushed history.
2. **Establish the baseline:**
   `git push origin demo-release-readiness`. That creates a branch and changes
   nothing else.
3. **Publish the harness:**
   `git push origin feat/autonomous-harness`. Still no PR and no
   default-branch change.
4. **Integrate, when ready:** fast-forward `main` to
   `demo-release-readiness` (`git push origin demo-release-readiness:main`).
   It is a fast-forward of linear history: no rewrite, no merge commit. Merge
   the harness through a reviewed PR afterwards, so CI runs on it.
5. **Last:** switch the default branch to `main`, which schedule and dispatch
   triggers need, and add a ruleset (require CI, no force-push, agents
   limited to `agent/*`). Keep `tollgate-2-hardening`; it is an ancestor and
   costs nothing.

## Open risks

These remain after Phase 1.5.

- **`ACP_SANDBOX=1` is a flag, not isolation.** It stops the verifier
  executing candidate code by accident, and it refuses on Windows outright.
  Isolation comes only from where the job runs (see "Properties the runner
  must enforce").
- **Malicious test code can fake its own result.** Conftest and startup
  files are protected (G3). Import-time tricks inside an allowed test file are
  not prevented. The git-only `check` verdict is unaffected. `criteria` and CI
  results are evidence, and a person reads the diff.
- **Shell patterns are a second layer.** An allowed interpreter can do
  anything the process can (G6: `ruff format <path>`, `pytest --junitxml=<path>`).
  On a disposable runner the blast radius is that runner. On a workstation it
  would be the user's account, which is why agents never run on one.
- **Egress is open on GitHub-hosted runners.** An agent job could send its own
  API key somewhere. It is bounded by the workspace spend limit and the absence
  of any GitHub token. Restricting it needs an egress policy or a self-hosted
  runner.
- **Hosted controls are documented, not tested.** Nothing here has run on
  GitHub Actions yet. Two things are unverified: hook behaviour on the runner
  without `--bare`, and how `permissions:` scopes the token. The pilot is where
  both are proved.
- **Approval is a label, not a signature.** It is as strong as the owner's
  GitHub account. Edits after approval revoke it; a compromised owner account
  is not covered.
- **Prompt injection through repo content** (docstrings, fixtures, docs) is
  discouraged by the agents' instructions and caught by the deterministic
  layers, not prevented.

## Repository facts the harness depends on

Checked 2026-11-22, read-only:

- **Remote branches:** `main` and `tollgate-2-hardening`, both at `63f332d`. `demo-release-readiness` is at `abcb04a`; `feat/autonomous-harness` is draft PR #1 against it.
- **Default branch:** `tollgate-2-hardening`. A `gh pr create` without `--base` would target it, so the harness always passes `--base` from `harness.toml`.
- **Protection:** no branch protection or rulesets. Auto-merge is off.
- **CI:** `.github/workflows/ci.yml` runs on `pull_request` (any base) and on `push` to `main`, with a `contents: read` token that is not left on disk. It runs on every push to PR #1.

Recommended changes. Only item 3 has been made; the rest are Paul's call.

1. Make `main` the default branch once the local branches are reconciled. It is
   what CI and `NEXT.md` already treat as the integration branch.
2. When protection is acceptable: require the `CI / quality` check and one
   review on `main`, and block force pushes.
3. ~~Add `permissions: contents: read` to `ci.yml`.~~ Done in `e322bc4`,
   together with `persist-credentials: false`.
4. Until `main` catches up, `--base main` is wrong. `main...HEAD` includes
   the 8 baseline commits (`CLAUDE.md`, `ci.yml`, `pyproject.toml` and more),
   so the scope check would correctly fail any task. The verifier takes a
   pinned SHA (`H`), never a branch name.
