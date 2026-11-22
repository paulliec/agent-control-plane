# Development harness

How an agent may work on this repo without someone watching, and what stops it
doing anything else. Status: **Phase 0/1.5** (`docs/NEXT.md` item 3): scaffolding
plus the Phase 1.5 security fixes. Nothing runs on a schedule, nothing
publishes, nothing opens a PR, and no workflow exists yet.

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

## Running unattended (once Phase 2 exists)

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

## Spending cap

`harness.toml` `max_budget_usd = 0` means *unset*, and a test pins it there.
Before any unattended run:

1. Run the pilot supervised and record what it cost (`total_cost_usd` in the JSON).
2. Set the cap to that cost plus headroom. Expect each overshoot to be up to one
   turn's cost; at today's prices that is cents, but it scales with context size.
3. Change the test on purpose, in the same commit as the cap.

`--max-budget-usd` applies per invocation, so a runner making several calls
(implementer, repairs, reviewer) must track the total itself.

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
| GitHub credential | held by an Anthropic proxy, outside the VM; **can push any branch** unless rulesets stop it | none in the agent job (`permissions: {}`, `persist-credentials: false`); a separate publish job gets a short-lived `GITHUB_TOKEN` | none |
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

### Proposed workflow shape (not written yet)

Four jobs. Only the last one can write to GitHub.

1. **`agent`**
   - **Token:** `permissions: {}`; checkout at the pinned base `H` with
     `persist-credentials: false`, so no git credentials are on disk.
   - **Branch:** creates `agent/<n>-<slug>`.
   - **Run:**
     `claude -p --settings .claude/harness/settings.json --setting-sources project --permission-mode dontAsk --permission-prompts none --max-turns 20 --max-budget-usd <cap>`.
     **Never `--bare`**: it skips hooks, including the guard.
   - **Secrets and limits:** the API key comes from a GitHub environment with
     a required reviewer, so Paul approves each run. `timeout-minutes` caps
     wall-clock time.
   - **Output:** a git bundle and the JSON transcript, as artifacts.
2. **`check`**
   - **Token:** `contents: read`, no secrets; checkout at `H`.
   - **Run:** `python -m scripts.harness.gate --base H --ref agent/... --bundle agent.bundle --contract ...`.
     That is git-only: pinned base, ancestry, the ref name, and scope.
   - **Trust:** it never executes candidate code, so its verdict is the
     trustworthy one.
3. **`criteria`**
   - **Token:** `contents: read`, no secrets; a fresh runner.
   - **Run:** `ACP_SANDBOX=1 ... gate --run-criteria`, which runs `verify.sh`
     and the pytest node.
   - **Trust:** this runs untrusted code, so its result is evidence, not a
     verdict. The runner's isolation is the protection, not the flag.
4. **`publish`**
   - **Condition:** needs `check` and `criteria`; gated by environment approval.
   - **Token:** `contents: write`; pushes **only** `refs/heads/agent/*` from
     the bundle.
   - **Not in scope yet:** PRs and labels are Phase 3.

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
| Only `agent/*` is ever pushed | the `publish` job's refspec now; a ruleset later (a repo setting) |
| A person approves each run | GitHub environment required reviewers (a repo setting) |
| Network egress limits | **not available** on standard hosted runners. The exposure is accepted and bounded: a spend-limited key and no GitHub token in the job. A third-party egress policy or self-hosted runner is optional later |
| Only the owner can trigger | the workflow's trigger and `if:` on `github.actor`; contract eligibility re-checks the owner |

## First supervised pilot: exact sequence

The pilot is run by hand on GitHub Actions while Paul watches. Every step that
publishes or changes a setting is a separate approval.

**Preparation (Paul):**
1. In the `agent control plane` worktree, commit or set aside the uncommitted
   README/`requirements.txt` work on `demo-release-readiness`. The harness
   branch doesn't depend on it.
2. Optionally remove the local path from `docs/QUESTIONS_FOR_PAUL.md` Q23
   before anything is published.
3. Publish the baseline and the harness branch (see "Publishing" below).

**Infrastructure (separate approvals):**
4. Create a dedicated Anthropic Console workspace with a low spend limit
   (suggested $5) and an API key in it.
5. Create a GitHub environment, `acp-agent`: required reviewer Paul, with the
   key as its only secret.
6. Add the four-job workflow on `feat/autonomous-harness`. It is triggered by
   `push` to `pilot/*`, because schedule and dispatch only run from the
   default branch. Set `max_budget_usd` in `harness.toml` and flip its test in
   the same commit.

**Run:**
7. Pin `H = git rev-parse feat/autonomous-harness` after step 6. Check it
   locally with `contract check-file docs/harness/pilot-demo-counts.md`
   (`eligible: true`).
8. Push `pilot/1` at `H`. Approve the `acp-agent` environment prompt and watch
   the `agent` job: at most 20 turns, within the cap.
9. `check` must report `ok: true`. `criteria` must report every automated
   criterion `pass`.
10. **Repair:** if `check` or `criteria` fails and the implementer didn't stop,
    re-run with the gate output, at most twice. After that, stop.
11. **Review:** Paul runs the reviewer (Read, Grep and Glob only) on the
    contract and the diff. Paul reads `gate.json`, the transcript and the
    diff, and decides the manual criteria.
12. **Publish:** approve `publish` only if satisfied. It pushes
    `agent/1-demo-counts` and nothing else. No PR is opened.
13. **Record:** cost, turns, denials, repairs and verdicts, in a pilot log
    under `docs/harness/`. Set the real budget cap from what the run cost.

**Abort on any of these:**
- a permission denial or guard block nobody expected
- `check` rejects the bundle
- spend above the cap
- the implementer edits anything outside the contract

If the counts disagree with `CURRENT_STATE.md` and the implementer stops, the
harness passed.

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
