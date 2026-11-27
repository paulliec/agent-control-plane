"""The pilot runner's controls, exercised for real against throwaway repos.

A bare `origin`, a trusted checkout at the pinned base, real git bundles, the
fake engine standing in for a model, and a fake GitHub that records every
comment. No network, no model, no money. Docker is not available here, so the
container itself is only exercised up to "no docker means no execution"; what
the container enforces is listed as unproven in docs/HARNESS.md.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from scripts.harness import (
    confidential,
    config,
    contract,
    gate,
    ledger,
    pilot,
    sandbox,
    scope,
)
from scripts.harness.config import REPO_ROOT, Budget

CONTRACT = "docs/harness/pilot-demo-counts.md"
OWNER = "paulliec"
APPROVED_AT = "2026-11-01T10:00:00Z"
CANARY = "ACP_CANARY_0123456789abcdef"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


class FakeGitHub:
    def __init__(self, body: str, *, fail_comments: bool = False) -> None:
        self.body = body
        self.posted: list[dict[str, Any]] = []
        self.fail_comments = fail_comments
        self.label_events = [
            {"action": "labeled", "label": "acp:approved", "actor": OWNER, "at": APPROVED_AT}
        ]

    def issue(self, number: int) -> dict[str, Any]:
        return {
            "number": number,
            "state": "OPEN",
            "author": OWNER,
            "body": self.body,
            "last_edited_at": None,
            "labels": ["acp:approved"],
            "label_events": self.label_events,
        }

    def comments(self, number: int) -> list[dict[str, Any]]:
        if self.fail_comments:
            raise RuntimeError("GitHub API unavailable")
        return list(self.posted)

    def comment(self, number: int, body: str) -> None:
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.posted.append(
            {"id": len(self.posted) + 1, "author": ledger.BOT, "body": body,
             "created_at": now, "updated_at": now}
        )  # fmt: skip


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    """origin (bare) + trusted checkout at H on the integration branch."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    root = tmp_path / "trusted"
    root.mkdir()
    git(root, "init", "-q", "-b", "demo-release-readiness")
    git(root, "config", "user.email", "t@example.invalid")
    git(root, "config", "user.name", "t")
    git(root, "config", "core.autocrlf", "false")
    for rel in ("harness.toml", "docs/NEXT.md", CONTRACT, "CLAUDE.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, root / rel)
    (root / "tests").mkdir()
    (root / "tests/test_existing.py").write_text("def test_base():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "demo-release-readiness")
    sha = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "--detach", sha)
    return {"root": root, "sha": sha, "cfg": config.load(root / "harness.toml"), "tmp": tmp_path}


def issue_body(sha: str, contract_text: str | None = None) -> str:
    text = contract_text or (REPO_ROOT / CONTRACT).read_text(encoding="utf-8")
    return f"{text}\n### Pinned base\n\n{sha}\n"


def run_for(sha: str, **kw: str) -> pilot.Run:
    fields = {"ref": "refs/heads/pilot/7", "sha": sha, "workflow_sha": sha, "run_id": "100"}
    fields |= {"attempt": "1", "actor": OWNER}
    fields.update(kw)
    return pilot.Run(**fields)


# --- trust anchor -------------------------------------------------------------


def test_an_approved_pinned_run_is_authorized_and_reserves_first(env: dict[str, Any]) -> None:
    gh = FakeGitHub(issue_body(env["sha"]))
    res = pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    assert res["base"] == env["sha"]
    records = ledger.parse(gh.posted)
    assert [r.kind for r in records] == ["reserve"]
    assert records[0].data["contract"] == res["contract_sha256"]
    assert records[0].data["implementer_usd"] + records[0].data["reviewer_usd"] == 2.75


@pytest.mark.parametrize(
    ("override", "why"),
    [
        ({"workflow_sha": "f" * 40}, "workflow and the code"),
        ({"actor": "someone-else"}, "run started by"),
    ],
)
def test_the_push_alone_does_not_establish_trust(
    env: dict[str, Any], override: dict[str, str], why: str
) -> None:
    gh = FakeGitHub(issue_body(env["sha"]))
    with pytest.raises(pilot.PilotError, match=why):
        pilot.authorize(run_for(env["sha"], **override), env["root"], env["cfg"], gh, CONTRACT)
    assert gh.posted == []  # nothing reserved, nothing spent


@pytest.mark.parametrize("ref", ["refs/heads/agent/7", "refs/heads/pilot/7x", "refs/tags/pilot/7"])
def test_only_pilot_refs_can_start_a_run(env: dict[str, Any], ref: str) -> None:
    gh = FakeGitHub(issue_body(env["sha"]))
    with pytest.raises(pilot.PilotError, match="is not refs/heads/pilot"):
        pilot.authorize(run_for(env["sha"], ref=ref), env["root"], env["cfg"], gh, CONTRACT)


def test_a_commit_that_never_reached_the_integration_branch_is_refused(
    env: dict[str, Any],
) -> None:
    root = env["root"]
    (root / "scripts").mkdir()
    (root / "scripts/evil.py").write_text("# an unreviewed runner change\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "unreviewed")
    sha = git(root, "rev-parse", "HEAD")  # committed and checked out, never pushed
    gh = FakeGitHub(issue_body(sha))
    with pytest.raises(pilot.PilotError, match="never reviewed"):
        pilot.authorize(run_for(sha), root, env["cfg"], gh, CONTRACT)


def test_the_issue_must_pin_the_pushed_commit(env: dict[str, Any]) -> None:
    gh = FakeGitHub(issue_body("a" * 40))
    with pytest.raises(pilot.PilotError, match="the issue pins"):
        pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)


def test_the_issue_contract_must_match_the_committed_one(env: dict[str, Any]) -> None:
    widened = (
        (REPO_ROOT / CONTRACT)
        .read_text(encoding="utf-8")
        .replace(
            "- tests/test_demo_counts.py",
            "- tests/test_demo_counts.py\n- src/agent_control_plane/demo.py",
        )
    )
    gh = FakeGitHub(issue_body(env["sha"], widened))
    with pytest.raises(pilot.PilotError, match="does not match"):
        pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)


def test_the_digest_ignores_rewrapping_but_not_meaning() -> None:
    cfg = config.load()
    text = (REPO_ROOT / CONTRACT).read_text(encoding="utf-8")
    rewrapped = text.replace("\n", "\r\n").replace(
        "regression test that pins", "regression test\nthat pins"
    )
    a, b = contract.parse(text, cfg), contract.parse(rewrapped, cfg)
    assert contract.digest(a) == contract.digest(b)
    changed = contract.parse(text.replace("166 events", "167 events"), cfg)
    assert contract.digest(changed) != contract.digest(a)


def test_a_dirty_trusted_checkout_is_refused(env: dict[str, Any]) -> None:
    (env["root"] / "harness.toml").write_text("# tampered\n")
    gh = FakeGitHub(issue_body(env["sha"]))
    with pytest.raises(pilot.PilotError, match="local changes"):
        pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)


def test_a_malformed_pinned_base_fails_the_contract(env: dict[str, Any]) -> None:
    with pytest.raises(contract.ContractError, match="pinned base"):
        contract.parse(issue_body("HEAD"), env["cfg"])


# --- budget and ledger ----------------------------------------------------------


def budget(**kw: float) -> Budget:
    base = {"task_usd": 3.0, "implementer_usd": 2.0, "reviewer_usd": 0.75, "ceiling_usd": 5.0}
    return Budget(**(base | kw))


@pytest.mark.parametrize(
    "b",
    [
        None,
        budget(task_usd=0),
        budget(implementer_usd=-1),
        budget(reviewer_usd=math.nan),
        budget(implementer_usd=2.5),  # 2.5 + 0.75 > 3
        budget(task_usd=6.0),  # over the ceiling
    ],
)
def test_missing_or_invalid_budgets_fail_closed(b: Budget | None) -> None:
    assert ledger.budget_problems(b)


def test_the_approved_budget_is_valid() -> None:
    assert ledger.budget_problems(config.load().budget) == []


def test_a_harness_toml_without_a_budget_table_refuses(env: dict[str, Any]) -> None:
    toml = env["root"] / "harness.toml"
    text = toml.read_text(encoding="utf-8")
    start = text.index("[budget]")
    toml.write_text(text[:start] + text[text.index("[sandbox]") :], encoding="utf-8")
    cfg = config.load(toml)
    assert cfg.budget is None
    problems = ledger.start_problems([], "d", datetime.now(UTC), cfg)
    assert problems == ["[budget] is missing or malformed in harness.toml"]


def rec(kind: str, at: str, **data: Any) -> dict[str, Any]:
    body = ledger.render({"type": kind, **data})
    return {"author": ledger.BOT, "body": body, "created_at": at}


def reserve(at: str, run_id: str = "1", attempt: str = "1") -> dict[str, Any]:
    return rec(
        "reserve", at, contract="D", base="B", run_id=run_id, run_attempt=attempt,
        implementer_usd=2.0, reviewer_usd=0.75,
    )  # fmt: skip


def acct(run_id: str, impl: Any, rev: Any, attempt: str = "1") -> dict[str, Any]:
    return rec(
        "account", "2026-11-02T00:00:00Z", run_id=run_id, run_attempt=attempt,
        implementer_cost_usd=impl, reviewer_cost_usd=rev,
    )  # fmt: skip


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        ([reserve("2026-11-01T11:00:00Z")], 2.75),  # cancelled or lost: full reservation
        ([reserve("2026-11-01T11:00:00Z"), acct("1", 0.4, 0.2)], 0.6),
        ([reserve("2026-11-01T11:00:00Z"), acct("1", None, 0.2)], 2.2),  # missing cost = full
        ([reserve("2026-11-01T11:00:00Z"), acct("1", 0.4, "not_run")], 0.4),
        ([reserve("2026-11-01T11:00:00Z"), acct("1", -5, True)], 2.75),  # nonsense = full
        ([reserve("2026-11-01T11:00:00Z"), acct("1", 2.4, 0.9)], 3.3),  # overshoot counts
    ],
)
def test_spend_counts_reservations_until_costs_are_known(
    comments: list[dict[str, Any]], expected: float
) -> None:
    assert ledger.spent(ledger.parse(comments), "D") == pytest.approx(expected)


def test_only_bot_records_count_and_a_corrupt_one_refuses() -> None:
    forged = reserve("2026-11-01T11:00:00Z") | {"author": "stranger"}
    assert ledger.parse([forged, {"author": "x", "body": "hi", "created_at": APPROVED_AT}]) == []
    corrupt = {
        "author": ledger.BOT,
        "body": ledger.MARKER + "\n```json\n{oops\n```",
        "created_at": APPROVED_AT,
    }
    with pytest.raises(ledger.LedgerError):
        ledger.parse([corrupt])


def test_one_attempt_per_approval_and_the_budget_still_binds() -> None:
    cfg = config.load()
    approved = datetime.fromisoformat("2026-11-01T10:00:00+00:00")
    used = ledger.parse([reserve("2026-11-01T11:00:00Z")])
    assert any("already been used" in p for p in ledger.start_problems(used, "D", approved, cfg))
    # re-approved later, but the cancelled attempt still counts in full
    reapproved = datetime.fromisoformat("2026-11-03T10:00:00+00:00")
    assert any("exhausted" in p for p in ledger.start_problems(used, "D", reapproved, cfg))
    # with the real (low) costs recorded, a second approved attempt fits
    settled = ledger.parse([reserve("2026-11-01T11:00:00Z"), acct("1", 0.1, 0.1)])
    assert ledger.start_problems(settled, "D", reapproved, cfg) == []


def test_duplicate_dispatch_is_refused_and_reserves_nothing(env: dict[str, Any]) -> None:
    gh = FakeGitHub(issue_body(env["sha"]))
    pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    with pytest.raises(pilot.PilotError, match="already been used"):
        pilot.authorize(run_for(env["sha"], run_id="101"), env["root"], env["cfg"], gh, CONTRACT)
    assert len(ledger.parse(gh.posted)) == 1


def test_unreadable_prior_state_refuses(env: dict[str, Any]) -> None:
    gh = FakeGitHub(issue_body(env["sha"]), fail_comments=True)
    with pytest.raises(RuntimeError, match="unavailable"):
        pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    assert gh.posted == []


def test_a_rerun_has_no_reservation_and_cannot_spend(env: dict[str, Any]) -> None:
    gh = FakeGitHub(issue_body(env["sha"]))
    pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    pilot.require_reservation(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    with pytest.raises(pilot.PilotError, match="no reservation"):
        pilot.require_reservation(
            run_for(env["sha"], attempt="2"), env["root"], env["cfg"], gh, CONTRACT
        )


def test_a_reservation_from_before_the_current_approval_is_stale(env: dict[str, Any]) -> None:
    gh = FakeGitHub(issue_body(env["sha"]))
    pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    gh.label_events.append(
        {"action": "labeled", "label": "acp:approved", "actor": OWNER, "at": "2099-01-01T00:00:00Z"}
    )
    with pytest.raises(pilot.PilotError, match="no reservation"):
        pilot.require_reservation(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)


# --- agent stage: isolation of the engine ------------------------------------------


def fake_script(tmp: Path, name: str, **script: Any) -> Path:
    p = tmp / f"{name}.json"
    p.write_text(json.dumps(script), encoding="utf-8")
    return p


def run_agent(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **script: Any
) -> tuple[dict[str, Any], FakeGitHub, Path]:
    run = run_for(env["sha"], ref="refs/heads/pilot-drill/7")
    gh = FakeGitHub(issue_body(env["sha"]))
    pilot.authorize(run, env["root"], env["cfg"], gh, CONTRACT)
    monkeypatch.setenv("AEH_FAKE_SCRIPT", str(fake_script(env["tmp"], "impl", **script)))
    out = env["tmp"] / "out"
    return pilot.agent(run, env["root"], env["cfg"], gh, CONTRACT, out), gh, out


def uploadable(out: Path) -> list[Path]:
    """Exactly what the agent job's upload step can pick up."""
    names = ("agent.json", "agent.bundle", "implementer-summary.json")
    return [out / n for n in names if (out / n).exists() and n != "agent.bundle"]


def test_the_engine_gets_no_github_token_or_host_secrets(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghs_" + "x" * 36)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "y" * 36)
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "z" * 30)  # not for the fake engine either
    monkeypatch.setenv("ACP_HOST_CANARY", CANARY)
    monkeypatch.setenv("AEH_FAKE_SCRIPT", str(fake_script(env["tmp"], "probe")))
    work, raw, prompt = env["tmp"] / "w", env["tmp"] / "raw.json", env["tmp"] / "p.md"
    work.mkdir()
    prompt.write_text("x")
    code, _ = pilot.run_engine("fake", "implementer", work, prompt, raw, 1, 0.1, "Read")
    seen = set(json.loads(raw.read_text())["env_names"])  # what the engine process really had
    assert code == 0
    assert not seen & {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ANTHROPIC_API_KEY",
        "ACP_HOST_CANARY",
    }
    # and through the agent stage, the raw transcript doesn't outlive the summary
    res, _, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": "def test_x():\n    pass\n"}
    )
    assert res["status"] == "committed"
    assert not (out / "implementer-run.json").exists()
    assert (out / "implementer-summary.json").exists()


def test_the_engine_never_writes_the_trusted_checkout(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, _, out = run_agent(env, monkeypatch, writes={"tests/test_demo_counts.py": "x = 1\n"})
    assert git(env["root"], "status", "--porcelain") == ""
    assert not (env["root"] / "tests/test_demo_counts.py").exists()
    assert gate.sha256_file(out / "agent.bundle") == res["bundle_sha256"]


def test_no_edits_means_no_bundle(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    res, _, out = run_agent(env, monkeypatch, writes={})
    assert res["status"] == "no_changes"
    assert not (out / "agent.bundle").exists()


def test_hooks_in_the_agent_git_dir_never_run(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = env["tmp"] / "hook-ran"
    # the engine runs before the commit; plant a hook where git would look for it
    hooks = env["tmp"] / "out" / "agent.git" / "hooks"
    script = {"writes": {"tests/test_demo_counts.py": "x = 1\n"}}
    run = run_for(env["sha"], ref="refs/heads/pilot-drill/7")
    gh = FakeGitHub(issue_body(env["sha"]))
    pilot.authorize(run, env["root"], env["cfg"], gh, CONTRACT)
    monkeypatch.setenv("AEH_FAKE_SCRIPT", str(fake_script(env["tmp"], "impl", **script)))
    real = pilot.run_engine

    def engine_then_plant(*args: Any, **kw: Any) -> tuple[int, str]:
        code = real(*args, **kw)
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "pre-commit").write_text(f"#!/bin/sh\necho ran > '{marker.as_posix()}'\n")
        (hooks / "pre-commit").chmod(0o755)
        return code

    monkeypatch.setattr(pilot, "run_engine", engine_then_plant)
    res = pilot.agent(run, env["root"], env["cfg"], gh, CONTRACT, env["tmp"] / "out")
    assert res["status"] == "committed"
    assert not marker.exists()


def test_the_real_engine_never_starts_without_its_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(pilot.PilotError, match="ANTHROPIC_API_KEY"):
        pilot.engine_env("claude")


def test_drills_and_real_runs_cannot_swap_engines() -> None:
    assert run_for("a" * 40).engine == "claude"
    assert run_for("a" * 40, ref="refs/heads/pilot-drill/7").engine == "fake"


# --- check: protected paths, secrets, artifact substitution -----------------------


def run_honestly(cmd: list[str], cwd: Path) -> tuple[int, str]:
    """A stand-in sandbox: pytest criteria really run (on the host, in a throwaway
    tree of trusted test content); anything else reports success."""
    if cmd[:3] != ["python", "-m", "pytest"]:
        return 0, "ok"
    proc = subprocess.run([sys.executable, *cmd[1:]], cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def check(env: dict[str, Any], res: dict[str, Any], out: Path, **kw: Any) -> dict[str, Any]:
    run = run_for(env["sha"], ref="refs/heads/pilot-drill/7")
    digest = kw.pop("digest", res["bundle_sha256"])
    tip = kw.pop("tip", res["tip"])
    return pilot.check(
        run,
        env["root"],
        CONTRACT,
        kw.pop("bundle", out / "agent.bundle"),
        digest,
        tip,
        out / "check",
    )


def test_a_benign_candidate_passes_the_trusted_check(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, _, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": "def test_x():\n    pass\n"}
    )
    report = check(env, res, out)
    assert report["ok"] is True
    assert report["scope"]["changed"] == ("tests/test_demo_counts.py",)
    assert (out / "check" / "diff.patch").read_text().count("test_demo_counts.py") >= 1


@pytest.mark.parametrize(
    "writes",
    [
        {"CLAUDE.md": "rules removed\n"},
        {"tests/conftest.py": "import pytest\n"},
        {"scripts/harness/gate.py": "print('ok')\n"},
        {"harness.toml": "\n"},
        {".github/workflows/agent-pilot.yml": "on: push\n"},
        {"docs/harness/pilot-demo-counts.md": "### Goal\n\nanything\n"},
        {"src/agent_control_plane/demo.py": "# out of contract\n"},
    ],
)
def test_protected_and_out_of_scope_edits_fail_the_check(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, writes: dict[str, str]
) -> None:
    # disable the pre-upload scope check to prove check doesn't depend on it
    no_scope = SimpleNamespace(**vars(scope))
    no_scope.check = lambda *a: scope.ScopeResult(("x",), (), ())
    monkeypatch.setattr(pilot, "scope", no_scope)
    writes = {"tests/test_demo_counts.py": "def test_x():\n    pass\n", **writes}
    res, _, out = run_agent(env, monkeypatch, writes=writes)
    report = check(env, res, out)
    assert report["ok"] is False


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("CLAUDE.md", "protected"),
        ("harness.toml", "protected"),
        (".github/workflows/agent-pilot.yml", "protected"),
        ("src/agent_control_plane/demo.py", "outside_contract"),
    ],
)
def test_an_out_of_scope_candidate_never_becomes_a_bundle(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, path: str, kind: str
) -> None:
    writes = {"tests/test_demo_counts.py": "def test_x():\n    pass\n", path: "changed\n"}
    res, _, out = run_agent(env, monkeypatch, writes=writes)
    assert res["status"] == "scope_blocked"
    assert path in res["scope"][kind]
    assert "tip" not in res and "bundle_sha256" not in res
    assert not (out / "agent.bundle").exists()


def test_a_secret_written_into_the_candidate_never_reaches_a_bundle(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, _, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": f"KEY = '{CANARY}'\n"}
    )
    assert res["status"] == "confidentiality_blocked"
    assert res["findings"] == ["harness-canary"]
    assert not (out / "agent.bundle").exists()
    for f in uploadable(out):
        assert CANARY not in f.read_text(encoding="utf-8"), f.name


def test_the_downstream_check_catches_a_secret_on_its_own(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # disable the pre-upload scan to prove check doesn't depend on it
    monkeypatch.setattr(confidential, "findings", lambda text, env=None: [])
    res, _, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": f"KEY = '{CANARY}'\n"}
    )
    report = check(env, res, out)
    assert report["ok"] is False
    assert report["secrets"] == [{"file": "tests/test_demo_counts.py", "pattern": "harness-canary"}]
    assert CANARY not in json.dumps(report)  # the report names the pattern, never the value


def test_a_substituted_bundle_or_tip_is_refused(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, _, out = run_agent(env, monkeypatch, writes={"tests/test_demo_counts.py": "x = 1\n"})
    with pytest.raises(gate.GateError, match="digest"):
        check(env, res, out, digest="0" * 64)
    with pytest.raises(gate.GateError, match="recorded tip"):
        check(env, res, out, tip="0" * 40)
    # a different bundle, same branch name: its digest gives it away
    other = env["tmp"] / "other"
    shutil.copytree(out / "agent.git", other / "agent.git")
    swapped = out / "swapped.bundle"
    shutil.copy(out / "agent.bundle", swapped)
    with swapped.open("ab") as f:
        f.write(b"\0")
    with pytest.raises(gate.GateError, match="digest"):
        check(env, res, out, bundle=swapped)


# --- criteria: baseline vs candidate, separate environments --------------------------


def test_baseline_and_candidate_run_in_separate_trees_with_the_right_tests(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, _, out = run_agent(
        env, monkeypatch,
        writes={"tests/test_demo_counts.py": "def test_x():\n    pass\n"},
    )  # fmt: skip
    calls: list[tuple[list[str], Path, set[str]]] = []

    def execute(cmd: list[str], cwd: Path) -> tuple[int, str]:
        files = {p.relative_to(cwd).as_posix() for p in (cwd / "tests").rglob("*.py")}
        calls.append((cmd, cwd, files))
        return run_honestly(cmd, cwd)

    monkeypatch.setattr(gate, "sandboxed", lambda: True)
    report = gate.evaluate(
        env["root"], env["sha"], "agent/7", CONTRACT, bundle=out / "agent.bundle",
        run_criteria=True, execute=execute, expect_bundle_sha256=res["bundle_sha256"],
    )  # fmt: skip
    baseline_cwd, cand_cwd = calls[0][1], calls[1][1]
    assert baseline_cwd != cand_cwd
    assert "tests/test_demo_counts.py" not in calls[0][2]  # baseline: base tests only
    assert "tests/test_existing.py" in calls[0][2]
    assert "tests/test_demo_counts.py" in calls[1][2]
    assert report["baseline"]["label"] == "trusted_baseline"
    assert report["criteria_label"] == "candidate_authored"
    assert report["ok"] is True


def test_a_candidate_that_fakes_its_own_pass_still_fails_on_the_baseline(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, out = run_agent(env, monkeypatch, writes={"tests/test_demo_counts.py": "x = 1\n"})
    seen: list[Path] = []

    def execute(cmd: list[str], cwd: Path) -> tuple[int, str]:
        seen.append(cwd)
        return (1, "baseline broke") if cwd.name == "baseline" else (0, "all green, honest")

    monkeypatch.setattr(gate, "sandboxed", lambda: True)
    report = gate.evaluate(
        env["root"], env["sha"], "agent/7", CONTRACT, bundle=out / "agent.bundle",
        run_criteria=True, execute=execute,
    )  # fmt: skip
    assert report["ok"] is False
    assert report["baseline"]["status"] == "fail"


def test_without_docker_candidate_code_never_runs_anywhere(tmp_path: Path) -> None:
    sb = config.load().sandbox
    assert sb is not None
    if shutil.which("docker"):
        pytest.skip("docker present: this checks the no-docker refusal only")
    marker = tmp_path / "ran"
    code, msg = sandbox.run(sb, tmp_path, tmp_path, ["sh", "-c", f"touch {marker.as_posix()}"])
    assert code in (126, 127), msg  # refused: no POSIX runner, or no docker
    assert "refusing" in msg
    assert not marker.exists()


def test_the_container_sees_no_host_value(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "z" * 30)
    monkeypatch.setenv("GH_TOKEN", "ghs_" + "x" * 36)
    monkeypatch.setenv("ACP_HOST_CANARY", CANARY)
    sb = config.load().sandbox
    assert sb is not None
    argv = sandbox.argv(sb, tmp_path, tmp_path, ["bash", "scripts/verify.sh"], "1001:1001")
    joined = " ".join(argv)
    for secret in ("sk-ant-", "ghs_", CANARY, "ANTHROPIC_API_KEY", "GH_TOKEN"):
        assert secret not in joined
    assert not {"ANTHROPIC_API_KEY", "GH_TOKEN", "ACP_HOST_CANARY"} & set(sandbox.client_env())
    i = argv.index("--network")
    assert argv[i + 1] == "none"
    for flag in ("--read-only", "--cap-drop", "--pids-limit", "--memory", "--cpus", "--user"):
        assert flag in argv
    assert argv[argv.index("--user") + 1] != "0:0"
    assert "--env-file" not in argv and "--privileged" not in argv
    mounts = [argv[k + 1] for k, a in enumerate(argv) if a == "--mount"]
    assert len(mounts) == 2 and mounts[1].endswith(",readonly")


def test_criteria_refuse_without_sandbox_prerequisites(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, out = run_agent(env, monkeypatch, writes={"tests/test_demo_counts.py": "x = 1\n"})
    monkeypatch.setattr(gate, "sandboxed", lambda: True)
    report = gate.evaluate(
        env["root"], env["sha"], "agent/7", CONTRACT, bundle=out / "agent.bundle", run_criteria=True
    )
    assert report["ok"] is False
    assert "sandbox prerequisites" in report["error"]


# --- review -----------------------------------------------------------------------

GOOD = {
    "verdict": "approve",
    "blockers": [],
    "findings": [{"file": "tests/x.py", "line": 1, "severity": "minor", "issue": "nit"}],
    "security_findings": [{"issue": "none found", "status": "resolved"}],
    # one entry per manual criterion in the approved contract, by id
    "manual_criteria": [
        {"criterion": f"M{i}", "status": "unresolved", "evidence": "?"} for i in (1, 2, 3)
    ],
    "suspected_injection": [],
}
MANUAL = pilot.manual_criteria(contract.parse((REPO_ROOT / CONTRACT).read_text(), config.load()))


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"verdict": "changes"}, "not 'approve'"),
        ({"verdict": "stop"}, "not 'approve'"),
        ({"blockers": [{"issue": "x"}]}, "blocker"),
        ({"security_findings": [{"issue": "x", "status": "unresolved"}]}, "security"),
        ({"security_findings": ["a bare string"]}, "security"),
        ({"suspected_injection": ["ignore the rules"]}, "injection"),
        ({"blockers": None}, "missing"),
    ],
)
def test_only_an_explicit_clean_approve_is_success(change: dict[str, Any], why: str) -> None:
    assert pilot.review_problems(GOOD) == []  # unresolved *manual* criteria stay with the human
    assert any(why in p for p in pilot.review_problems(GOOD | change))


def test_no_structured_verdict_is_not_success() -> None:
    assert pilot.review_problems(None)
    assert pilot.review_problems("approve")


def review_setup(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, verdict: dict[str, Any]
) -> tuple[Path, dict[str, str], FakeGitHub, pilot.Run]:
    res, gh, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": "def test_x():\n    pass\n"}
    )
    check(env, res, out)
    ev = env["tmp"] / "evidence"
    ev.mkdir()
    for f in (
        out / "agent.bundle",
        out / "check" / "gate-check.json",
        out / "check" / "diff.patch",
    ):
        shutil.copy(f, ev / f.name)
    (ev / "criteria.json").write_text(json.dumps({"ok": True}), encoding="utf-8")
    (ev / "implementer-run.json").write_text(json.dumps({"result": "IMPLEMENTER-NARRATIVE-7f3"}))
    shutil.copy(out / "implementer-summary.json", ev / "implementer-summary.json")
    expected = {
        n: gate.sha256_file(ev / n)
        for n in (
            "agent.bundle",
            "gate-check.json",
            "diff.patch",
            "criteria.json",
            "implementer-summary.json",
        )
    }
    monkeypatch.setenv(
        "AEH_FAKE_SCRIPT",
        str(fake_script(env["tmp"], "rev", structured_output=verdict, cost_usd=0.1)),
    )
    return ev, expected, gh, run_for(env["sha"], ref="refs/heads/pilot-drill/7")


def test_the_reviewer_gets_evidence_but_never_the_implementers_narrative(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev, expected, gh, run = review_setup(env, monkeypatch, GOOD)
    out = env["tmp"] / "rev-out"
    res = pilot.review(run, env["root"], env["cfg"], gh, CONTRACT, ev, expected, out)
    prompt = (out / "reviewer-prompt.md").read_text(encoding="utf-8")
    assert "IMPLEMENTER-NARRATIVE-7f3" not in prompt
    assert "<<<UNTRUSTED_DIFF" in prompt and "<<<TRUSTED_GATE_CHECK" in prompt
    assert "candidate_authored" in prompt
    assert res["accepted"] is True


def test_the_reviewer_refuses_substituted_evidence(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev, expected, gh, run = review_setup(env, monkeypatch, GOOD)
    (ev / "criteria.json").write_text(json.dumps({"ok": True, "forged": 1}), encoding="utf-8")
    with pytest.raises(pilot.PilotError, match=r"criteria\.json"):
        pilot.review(
            run, env["root"], env["cfg"], gh, CONTRACT, ev, expected, env["tmp"] / "rev-out"
        )


def test_a_changes_verdict_is_recorded_as_not_accepted(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev, expected, gh, run = review_setup(env, monkeypatch, GOOD | {"verdict": "changes"})
    res = pilot.review(
        run, env["root"], env["cfg"], gh, CONTRACT, ev, expected, env["tmp"] / "rev-out"
    )
    assert res["accepted"] is False


# --- accounting and the final verdict ---------------------------------------------------


def test_account_records_missing_costs_as_unknown_and_skipped_review_as_not_run(
    tmp_path: Path,
) -> None:
    gh = FakeGitHub("")
    body = pilot.account(run_for("a" * 40), gh, tmp_path, "skipped", {})
    data = json.loads(body.split("```json\n")[1].split("\n```")[0])
    assert data["implementer_cost_usd"] is None
    assert data["reviewer_cost_usd"] == "not_run"


def evidence(tmp: Path, **override: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    tip = "c" * 40
    files = {
        "agent.json": {
            "status": "committed",
            "tip": tip,
            "engine_complete": True,
            "contract_sha256": "d" * 64,
        },
        "implementer-summary.json": {"complete": True, "total_cost_usd": 0.5},
        "reviewer-summary.json": {"complete": True, "total_cost_usd": 0.1},
        "gate-check.json": {
            "ok": True,
            "tip": tip,
            "bundle_sha256": "b" * 64,
            "contract_sha256": "d" * 64,
        },
        "criteria.json": {
            "ok": True,
            "criteria_run": True,
            "tip": tip,
            "baseline": {"label": "trusted_baseline", "status": "pass"},
        },
        "review.json": {
            "accepted": True,
            "problems": [],
            "verdict": GOOD,
            "engine_invoked": True,
            "engine_complete": True,
        },
    } | override
    ev = tmp / "ev"
    ev.mkdir(exist_ok=True)
    for name, data in files.items():
        (ev / name).write_text(json.dumps(data), encoding="utf-8")
    return ev, {n: gate.sha256_file(ev / n) for n in files}


def test_a_fully_verified_run_is_ready_for_a_human_and_lists_manual_criteria(
    tmp_path: Path,
) -> None:
    ev, expected = evidence(tmp_path)
    result = pilot.verdict(ev, expected)
    assert result["ready_for_human"] is True
    # every manual criterion of the approved contract, left for the human, with
    # the reviewer's assessment alongside
    assert result["manual_criteria_for_human"] == [
        {"id": mid, "criterion": text, "status": "pending", "reviewer": entry}
        for (mid, text), entry in zip(MANUAL, GOOD["manual_criteria"], strict=True)
    ]


@pytest.mark.parametrize(
    "override",
    [
        {"review.json": {"accepted": False, "problems": ["reviewer verdict is 'changes'"]}},
        {
            "criteria.json": {
                "ok": True,
                "criteria_run": True,
                "tip": "c" * 40,
                "baseline": {"label": "candidate_authored"},
            }
        },
        {"gate-check.json": {"ok": True, "tip": "e" * 40}},
        {"agent.json": {"status": "no_changes"}},
    ],
)
def test_anything_short_of_full_verification_is_not_ready(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    ev, expected = evidence(tmp_path, **override)
    assert pilot.verdict(ev, expected)["ready_for_human"] is False


def test_a_substituted_evidence_file_fails_the_verdict(tmp_path: Path) -> None:
    ev, expected = evidence(tmp_path)
    (ev / "review.json").write_text(json.dumps({"accepted": True}), encoding="utf-8")
    with pytest.raises(pilot.PilotError, match=r"review\.json"):
        pilot.verdict(ev, expected)


def test_a_stage_that_never_ran_counts_as_absent_evidence(tmp_path: Path) -> None:
    ev, expected = evidence(tmp_path)
    expected["review.json"] = ""  # review was skipped; a stray file must not count
    assert pilot.verdict(ev, expected)["ready_for_human"] is False


# --- the workflow itself -----------------------------------------------------------------

WORKFLOW = REPO_ROOT / ".github/workflows/agent-pilot.yml"


@pytest.fixture(scope="module")
def wf() -> dict[str, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_no_job_can_write_contents_and_only_two_can_write_issues(wf: dict[str, Any]) -> None:
    assert wf["permissions"] == {}
    for name, job in wf["jobs"].items():
        perms = job.get("permissions")
        assert isinstance(perms, dict), name  # every job states what it needs
        assert perms.get("contents") in (None, "read"), name
        assert perms.get("issues") in (None, "read", "write"), name
        assert not set(perms) - {"contents", "issues"}, name
        if perms.get("issues") == "write":
            assert name in ("authorize", "account"), name


def test_secrets_reach_only_the_model_jobs_and_never_the_sandbox(wf: dict[str, Any]) -> None:
    for name, job in wf["jobs"].items():
        text = yaml.safe_dump(job)
        if "secrets." in text:
            assert name in ("agent", "review"), name
            assert "acp-agent" in str(job.get("environment")), name
    crit = wf["jobs"]["criteria"]
    assert "environment" not in crit
    assert "secrets." not in yaml.safe_dump(crit)
    assert any(s.get("env", {}).get("ACP_SANDBOX") == "1" for s in crit["steps"])


def test_every_checkout_is_the_pinned_commit_without_credentials(wf: dict[str, Any]) -> None:
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            uses = str(step.get("uses", ""))
            if uses:
                ref = uses.split("@", 1)[1].split()[0]
                assert len(ref) == 40 and all(c in "0123456789abcdef" for c in ref), (name, uses)
            if uses.startswith("actions/checkout"):
                assert step["with"]["ref"] == "${{ github.sha }}", name
                assert step["with"]["persist-credentials"] is False, name
            if uses.startswith("actions/setup-python"):
                assert "cache" not in step.get("with", {}), name


def test_nothing_in_the_workflow_publishes(wf: dict[str, Any]) -> None:
    # what actually executes: every run: command in every job
    commands = [str(s["run"]) for job in wf["jobs"].values() for s in job["steps"] if "run" in s]
    for cmd in commands:
        for banned in ("git push", "gh ", "--bare", "merge", "curl", "wget"):
            assert banned not in cmd, cmd
    # and nothing outside comments asks for a write scope
    code = "\n".join(
        line.split("#", 1)[0] for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
    )
    assert "pull-requests" not in code and "contents: write" not in code
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert wf["jobs"]["authorize"]["if"] == "github.actor == 'paulliec'"
    assert wf["jobs"]["account"]["if"].startswith("always()")


def test_the_engine_has_no_shell_and_the_adapter_keeps_its_hooks() -> None:
    assert "Bash" not in pilot.IMPLEMENTER_TOOLS and "PowerShell" not in pilot.IMPLEMENTER_TOOLS
    assert set(pilot.REVIEWER_TOOLS.split(",")) == {"Read", "Grep", "Glob"}
    adapter = (REPO_ROOT / "scripts/harness/engines/claude.sh").read_text(encoding="utf-8")
    assert "--bare" not in adapter.replace("Never --bare", "")
    assert '--settings "$settings"' in adapter and '--tools "$tools"' in adapter


def test_the_sandbox_image_is_pinned_by_digest() -> None:
    sb = config.load().sandbox
    assert sb is not None
    name, _, digest = sb.image.partition("@sha256:")
    assert name and len(digest) == 64


# --- self-review additions: races, tampering, pinning ---------------------------------


class RacingGitHub(FakeGitHub):
    """Another run's reservation lands between this run's check and its write."""

    def comment(self, number: int, body: str) -> None:
        if not self.posted:
            data = json.loads(body.split("```json\n")[1].split("\n```")[0])
            rival = ledger.render(data | {"run_id": "999"})
            super().comment(number, rival)
        super().comment(number, body)


def test_two_runs_racing_for_one_approval_only_the_first_proceeds(env: dict[str, Any]) -> None:
    gh = RacingGitHub(issue_body(env["sha"]))
    with pytest.raises(pilot.PilotError, match="another run reserved this approval first"):
        pilot.authorize(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    records = ledger.parse(gh.posted)
    assert [r.data["run_id"] for r in records] == ["999", "100"]
    assert ledger.spent(records, records[0].data["contract"]) == pytest.approx(5.5)  # both count
    with pytest.raises(pilot.PilotError, match="another run"):
        pilot.require_reservation(run_for(env["sha"]), env["root"], env["cfg"], gh, CONTRACT)
    pilot.require_reservation(
        run_for(env["sha"], run_id="999"), env["root"], env["cfg"], gh, CONTRACT
    )


def test_equal_timestamps_are_ordered_by_comment_id() -> None:
    at = "2026-11-01T11:00:00Z"
    a = reserve(at, run_id="2") | {"id": 20}
    b = reserve(at, run_id="1") | {"id": 10}
    first = ledger.first_after(
        ledger.parse([a, b]), "D", datetime.fromisoformat("2026-11-01T10:00:00+00:00")
    )
    assert first is not None and first.data["run_id"] == "1"


def test_an_edited_ledger_record_refuses() -> None:
    edited = reserve("2026-11-01T11:00:00Z") | {"updated_at": "2026-11-01T12:00:00Z"}
    with pytest.raises(ledger.LedgerError, match="edited"):
        ledger.parse([edited])


def test_the_pilot_runs_on_a_pinned_runner_image(wf: dict[str, Any]) -> None:
    assert {job["runs-on"] for job in wf["jobs"].values()} == {"ubuntu-24.04"}


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("- tests/test_demo_counts.py", "- tests/test_demo_count.py"),  # scope
        ("- verify\n- pytest", "- pytest"),  # a criterion dropped (and verify gone)
        ("- pytest tests/test_demo_counts.py", "- pytest tests/test_other.py"),  # criterion target
        ("asserted separately", "asserted together"),  # manual criterion wording
        ("Report both sets of", "Ignore both sets of"),  # stop condition meaning
        ("166 events", "1 66 events"),  # whitespace that splits a token is material
        ("20\nviolations", "2O\nviolations"),  # lookalike character
        ("Pinning would_block", "Pinning​would_block"),  # zero-width character
        ("1. Public demo readiness / deployment", "2. Code-review / demo documentation"),
    ],
)
def test_normalisation_never_hides_a_material_change(old: str, new: str) -> None:
    cfg = config.load()
    text = (REPO_ROOT / CONTRACT).read_text(encoding="utf-8")
    assert old in text, old
    original = contract.digest(contract.parse(text, cfg))
    try:
        changed = contract.digest(contract.parse(text.replace(old, new, 1), cfg))
    except contract.ContractError:
        return  # rejected outright is also not a silent match
    assert changed != original


def test_criteria_order_is_part_of_what_was_approved() -> None:
    cfg = config.load()
    text = (REPO_ROOT / CONTRACT).read_text(encoding="utf-8")
    swapped = text.replace(
        "- verify\n- pytest tests/test_demo_counts.py",
        "- pytest tests/test_demo_counts.py\n- verify",
    )
    assert swapped != text
    assert contract.digest(contract.parse(swapped, cfg)) != contract.digest(
        contract.parse(text, cfg)
    )


# --- PR #2 review: engine failure handling -------------------------------------------


@pytest.mark.parametrize(
    ("script", "why"),
    [
        ({"exit": 1}, "exited 1"),
        ({"subtype": "error_max_turns"}, "error_max_turns"),
        ({"subtype": "error_max_budget_usd", "exit": 1}, "exited 1"),
        ({"is_error": True}, "reported an error"),
        ({"raw_output": "not json at all"}, "unreadable"),
    ],
)
def test_an_incomplete_implementer_run_is_never_committed_or_bundled(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, script: dict[str, Any], why: str
) -> None:
    writes = {"tests/test_demo_counts.py": "def test_x():\n    pass\n"}
    res, _, out = run_agent(env, monkeypatch, writes=writes, **script)
    assert res["status"] == "engine_incomplete"
    assert res["engine_complete"] is False
    assert why in res["reason"]
    assert not (out / "agent.bundle").exists()
    summary = json.loads((out / "implementer-summary.json").read_text(encoding="utf-8"))
    assert summary["complete"] is False  # diagnostics kept, readiness not claimed


def test_a_failed_reviewer_is_not_an_approval_even_with_an_approving_verdict(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev, expected, gh, run = review_setup(env, monkeypatch, GOOD)
    monkeypatch.setenv(
        "AEH_FAKE_SCRIPT",
        str(fake_script(env["tmp"], "rev-fail", structured_output=GOOD, exit=1)),
    )
    res = pilot.review(run, env["root"], env["cfg"], gh, CONTRACT, ev, expected, env["tmp"] / "r")
    assert res["accepted"] is False
    assert res["verdict"] is None
    assert any("reviewer engine incomplete" in p for p in res["problems"])


@pytest.mark.parametrize(
    "override",
    [
        {"agent.json": {"status": "committed", "tip": "c" * 40, "engine_complete": False}},
        {
            "review.json": {
                "accepted": True,
                "problems": [],
                "verdict": GOOD,
                "engine_invoked": True,
                "engine_complete": False,
            }
        },
    ],
)
def test_the_verdict_requires_both_engines_to_have_completed(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    ev, expected = evidence(tmp_path, **override)
    assert pilot.verdict(ev, expected)["ready_for_human"] is False


# --- PR #2 review: pre-upload confidentiality ------------------------------------------


def test_engine_output_is_redacted_and_the_raw_transcript_is_not_kept(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, _, out = run_agent(
        env,
        monkeypatch,
        writes={"tests/test_demo_counts.py": "def test_x():\n    pass\n"},
        result=f"done; the key was {CANARY}",
        stderr=f"debug: {CANARY}",
    )
    assert res["status"] == "committed"
    assert not (out / "implementer-run.json").exists()
    for f in uploadable(out):
        assert CANARY not in f.read_text(encoding="utf-8"), f.name
    summary = (out / "implementer-summary.json").read_text(encoding="utf-8")
    assert confidential.REDACTED in summary


def test_a_held_credential_with_no_recognisable_shape_is_still_blocked(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    plain = "plain-credential-value-without-a-prefix-0042"
    monkeypatch.setenv("ANTHROPIC_API_KEY", plain)  # held by the job, never by the fake engine
    res, _, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": f"X = '{plain}'\n"}
    )
    assert res["status"] == "confidentiality_blocked"
    assert res["findings"] == ["held-credential"]
    for f in uploadable(out):
        assert plain not in f.read_text(encoding="utf-8"), f.name


def test_redaction_covers_patterns_and_held_values_but_not_short_ones() -> None:
    held = {"GH_TOKEN": "a-long-opaque-token-value", "GITHUB_TOKEN": "short"}
    text = f"{CANARY} and a-long-opaque-token-value and short"
    assert confidential.findings(text, held) == ["harness-canary", "held-credential"]
    assert confidential.redact(text, held) == "[REDACTED] and [REDACTED] and short"
    nested = confidential.redact_obj({"a": [f"x {CANARY}"]}, held)
    assert nested == {"a": ["x [REDACTED]"]}


def test_no_job_uploads_a_raw_engine_transcript(wf: dict[str, Any]) -> None:
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/upload-artifact"):
                assert "-run.json" not in str(step["with"]["path"]), name


# --- PR #2 review: cumulative runtime budget -----------------------------------------------


def budget_review(
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    implementer_cost: float | None,
    prior: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    ev, expected, gh, run = review_setup(env, monkeypatch, GOOD)
    gh.posted[:0] = prior or []
    summary = ev / "implementer-summary.json"
    data = json.loads(summary.read_text(encoding="utf-8")) | {"total_cost_usd": implementer_cost}
    summary.write_text(json.dumps(data), encoding="utf-8")
    expected["implementer-summary.json"] = gate.sha256_file(summary)
    called: list[str] = []
    real = pilot.run_engine

    def tracked(*args: Any, **kw: Any) -> tuple[int, str]:
        called.append(args[1])
        return real(*args, **kw)

    monkeypatch.setattr(pilot, "run_engine", tracked)
    res = pilot.review(run, env["root"], env["cfg"], gh, CONTRACT, ev, expected, env["tmp"] / "r")
    return res, called


def test_no_reviewer_is_paid_for_when_the_implementer_used_the_headroom(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, called = budget_review(env, monkeypatch, implementer_cost=2.4)  # 3 - 2.4 < 0.75
    assert called == []
    assert res["engine_invoked"] is False
    assert res["accepted"] is False
    assert res["budget"]["sufficient"] is False
    assert any("insufficient budget" in p for p in res["problems"])


def test_an_unknown_implementer_cost_counts_at_its_full_allocation(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    res, called = budget_review(env, monkeypatch, implementer_cost=None)  # counted as 2.00
    assert res["budget"]["implementer_usd"] == 2.0
    assert res["budget"]["implementer_cost_known"] is False
    assert called == ["reviewer"]


def earlier_attempt(digest: str, base: str, impl: Any, rev: Any) -> list[dict[str, Any]]:
    """A settled attempt from before the current approval."""
    return [
        rec(
            "reserve", "2026-10-30T10:00:00Z", contract=digest, base=base, run_id="50",
            run_attempt="1", implementer_usd=2.0, reviewer_usd=0.75,
        ),
        acct("50", impl, rev),
    ]  # fmt: skip


def test_spend_from_earlier_attempts_is_part_of_the_reconciliation(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    digest = contract.digest(contract.parse(issue_body(env["sha"]), env["cfg"]))
    earlier = earlier_attempt(digest, env["sha"], 0.1, 0.1)
    # 3 - 0.2 earlier - 2.1 now = 0.7 < 0.75; without the earlier attempt it would be 0.9
    res, called = budget_review(env, monkeypatch, implementer_cost=2.1, prior=earlier)
    assert res["budget"]["prior_usd"] == pytest.approx(0.2)
    assert called == []


def test_the_verdict_reports_final_spend_and_refuses_an_overrun(tmp_path: Path) -> None:
    ev, expected = evidence(
        tmp_path,
        **{
            "implementer-summary.json": {"complete": True, "total_cost_usd": 2.6},
            "reviewer-summary.json": {"complete": True, "total_cost_usd": 0.6},
        },
    )
    result = pilot.verdict(ev, expected)
    assert result["budget"]["spent_usd"] == pytest.approx(3.2)
    assert result["budget"]["over_budget"] is True
    assert result["ready_for_human"] is False


def test_the_verdict_counts_unknown_costs_conservatively(tmp_path: Path) -> None:
    unknown: dict[str, dict[str, Any]] = {
        "implementer-summary.json": {"complete": True, "total_cost_usd": None},
        "reviewer-summary.json": {"complete": True},
    }
    ev, expected = evidence(tmp_path, **unknown)
    budget = pilot.verdict(ev, expected)["budget"]
    assert budget["spent_usd"] == pytest.approx(2.75)
    assert budget["costs_known"] is False


def test_the_verdict_includes_earlier_attempts(tmp_path: Path) -> None:
    ev, expected = evidence(tmp_path)  # this attempt: 0.5 + 0.1
    unsettled = earlier_attempt("d" * 64, "a" * 40, None, None)[:1]  # reservation, no account
    records = ledger.parse(unsettled)
    result = pilot.verdict(ev, expected, records=records, run=run_for("a" * 40))
    assert result["budget"]["prior_usd"] == pytest.approx(2.75)  # counted in full
    assert result["budget"]["over_budget"] is True
    assert result["ready_for_human"] is False


# --- PR #2 review: concurrency and criteria isolation ----------------------------------------


def test_one_pilot_at_a_time_across_the_repository(wf: dict[str, Any]) -> None:
    assert wf["concurrency"] == {"group": "acp-pilot", "cancel-in-progress": False}


def test_each_executable_criterion_gets_a_fresh_tree(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": "def test_x():\n    pass\n"}
    )
    seen: list[tuple[Path, bool]] = []

    def execute(cmd: list[str], cwd: Path) -> tuple[int, str]:
        seen.append((cwd, (cwd / "tampered.txt").exists()))
        (cwd / "tampered.txt").write_text("left behind by the previous criterion")
        (cwd / "tests/test_demo_counts.py").write_text("def test_x():\n    assert False\n")
        return 0, "ok"

    monkeypatch.setattr(gate, "sandboxed", lambda: True)
    gate.evaluate(
        env["root"], env["sha"], "agent/7", CONTRACT, bundle=out / "agent.bundle",
        run_criteria=True, execute=execute,
    )  # fmt: skip
    candidate_runs = seen[1:]  # seen[0] is the trusted baseline
    assert len(candidate_runs) == 2  # verify and the pytest node
    assert len({cwd for cwd, _ in seen}) == len(seen)
    assert not any(tampered for _, tampered in seen)


# --- PR #2 re-review: reviewer redaction, bound cost evidence -------------------------


def test_the_whole_review_result_is_redacted_including_problems(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # a verdict value the harness quotes back into its own problem list
    ev, expected, gh, run = review_setup(env, monkeypatch, GOOD | {"verdict": CANARY})
    out = env["tmp"] / "r"
    res = pilot.review(run, env["root"], env["cfg"], gh, CONTRACT, ev, expected, out)
    assert res["accepted"] is False
    assert CANARY not in json.dumps(res)
    assert CANARY not in (out / "review.json").read_text(encoding="utf-8")
    assert any(confidential.REDACTED in p for p in res["problems"])


def account_evidence(tmp: Path) -> tuple[Path, dict[str, str]]:
    ev = tmp / "acct"
    ev.mkdir()
    files = {
        "implementer-summary.json": {"complete": True, "total_cost_usd": 0.5},
        "reviewer-summary.json": {"complete": True, "total_cost_usd": 0.25},
        "review.json": {"engine_invoked": True, "accepted": True},
    }
    for name, data in files.items():
        (ev / name).write_text(json.dumps(data), encoding="utf-8")
    return ev, {n: gate.sha256_file(ev / n) for n in files}


def recorded(ev: Path, expected: dict[str, str], review_result: str = "success") -> dict[str, Any]:
    body = pilot.account(run_for("a" * 40), FakeGitHub(""), ev, review_result, expected)
    data: dict[str, Any] = json.loads(body.split("```json\n")[1].split("\n```")[0])
    return data


def test_account_takes_costs_only_from_digest_bound_summaries(tmp_path: Path) -> None:
    ev, expected = account_evidence(tmp_path)
    data = recorded(ev, expected)
    assert (data["implementer_cost_usd"], data["reviewer_cost_usd"]) == (0.5, 0.25)


@pytest.mark.parametrize("name", ["implementer-summary.json", "reviewer-summary.json"])
@pytest.mark.parametrize("how", ["forged", "unbound", "missing"])
def test_forged_or_unbound_cost_evidence_counts_at_full_allocation(
    tmp_path: Path, name: str, how: str
) -> None:
    ev, expected = account_evidence(tmp_path)
    if how == "forged":  # a cheaper figure than the producing job recorded
        (ev / name).write_text(json.dumps({"total_cost_usd": 0.0}), encoding="utf-8")
    elif how == "unbound":
        expected[name] = ""
    else:
        (ev / name).unlink()
    data = recorded(ev, expected)
    impl_side = name.startswith("implementer")
    assert data["implementer_cost_usd" if impl_side else "reviewer_cost_usd"] is None
    # and the ledger then counts that side at its full reservation
    reserve = {"contract": "d", "run_id": data["run_id"], "run_attempt": data["run_attempt"],
               "implementer_usd": 2.0, "reviewer_usd": 0.75}  # fmt: skip
    records = [
        ledger.Record("reserve", datetime.now(UTC), reserve),
        ledger.Record("account", datetime.now(UTC), data, 1),
    ]
    want = 2.0 + 0.25 if impl_side else 0.5 + 0.75
    assert ledger.spent(records, "d") == pytest.approx(want)


def test_an_unbound_review_record_cannot_claim_the_reviewer_never_ran(tmp_path: Path) -> None:
    ev, expected = account_evidence(tmp_path)
    (ev / "reviewer-summary.json").unlink()
    (ev / "review.json").write_text(json.dumps({"engine_invoked": False}), encoding="utf-8")
    assert recorded(ev, expected)["reviewer_cost_usd"] is None  # not "not_run", i.e. $0
    expected["review.json"] = gate.sha256_file(ev / "review.json")
    assert recorded(ev, expected)["reviewer_cost_usd"] == "not_run"


def test_the_account_job_binds_its_cost_evidence(wf: dict[str, Any]) -> None:
    run = " ".join(s.get("run", "") for s in wf["jobs"]["account"]["steps"])
    for name, src in (
        ("implementer-summary.json", "needs.agent.outputs.summary_sha256"),
        ("review.json", "needs.review.outputs.review_sha256"),
        ("reviewer-summary.json", "needs.review.outputs.summary_sha256"),
    ):
        assert f'--expect "{name}=${{{{ {src} }}}}"' in run


# --- PR #2 re-review: a zero exit is not a test run; manual criteria from the contract ----

TARGET = "tests/test_demo_counts.py"
PYTEST = contract.Criterion("pytest", TARGET)


def candidate_tree(tmp: Path, test_source: str) -> Path:
    tree = tmp / "cand"
    (tree / "tests").mkdir(parents=True)
    (tree / "tests" / "test_demo_counts.py").write_text(test_source, encoding="utf-8")
    return tree


def test_a_premature_process_exit_is_not_a_pass(tmp_path: Path) -> None:
    # the finding: pytest exits 0 when a test module calls os._exit(0) at import
    tree = candidate_tree(tmp_path, "import os\n\nos._exit(0)\n")
    res = gate.run_criterion(PYTEST, config.load(), tree, run_honestly)
    assert res["exit_code"] == 0  # the exit code alone would have said pass
    assert res["status"] == "fail"
    assert res["report"]["problems"] == ["no test report: the run ended before pytest finished"]


def test_a_real_passing_run_of_the_requested_file_passes(tmp_path: Path) -> None:
    tree = candidate_tree(tmp_path, "def test_counts() -> None:\n    assert 166 == 143 + 10 + 13\n")
    res = gate.run_criterion(PYTEST, config.load(), tree, run_honestly)
    assert res["status"] == "pass"
    assert res["report"]["matched"] == 1
    assert not list(tree.glob(".acp-junit-*.xml"))  # consumed, never left in the tree


@pytest.mark.parametrize(
    ("source", "problem"),
    [
        ("def test_a() -> None:\n    assert False\n", "failures or errors"),
        ("import pytest\n\n\n@pytest.mark.skip\ndef test_a() -> None:\n    pass\n", "no passing"),
    ],
)
def test_real_failed_or_skipped_runs_fail(tmp_path: Path, source: str, problem: str) -> None:
    res = gate.run_criterion(PYTEST, config.load(), candidate_tree(tmp_path, source), run_honestly)
    assert res["status"] == "fail"
    assert any(problem in p for p in res["report"]["problems"])


def forging(xml: str, code: int = 0) -> gate.Executor:
    """An executor that writes `xml` (with {nonce} filled in) as the report."""

    def execute(cmd: list[str], cwd: Path) -> tuple[int, str]:
        path = next(a.split("=", 1)[1] for a in cmd if a.startswith("--junitxml="))
        nonce = next(a.split("=", 1)[1] for a in cmd if a.startswith("junit_suite_name="))
        (cwd / path).write_text(xml.replace("{nonce}", nonce), encoding="utf-8")
        return code, "1 passed"

    return execute


def suite(cases: str, tests: int, name: str = "{nonce}", failures: int = 0) -> str:
    return (
        f'<testsuites><testsuite name="{name}" tests="{tests}" failures="{failures}" '
        f'errors="0" skipped="0">{cases}</testsuite></testsuites>'
    )


OK_CASE = '<testcase classname="tests.test_demo_counts" name="test_counts" />'


@pytest.mark.parametrize(
    ("xml", "problem"),
    [
        (suite(OK_CASE, 1, name="an-earlier-run"), "not from this execution"),  # stale
        ("<testsuites><testsuite", "malformed"),
        ("<testsuites />", "no test suite"),
        (suite("", 0), "lists no tests"),
        (suite(OK_CASE, 3), "incomplete"),
        (suite(OK_CASE.replace("test_demo_counts", "test_other"), 1), "no passing test belongs"),
        (
            suite(OK_CASE.replace(" />", "><failure message='x' /></testcase>"), 1, failures=1),
            "failures or errors",
        ),
        (suite(OK_CASE.replace(" />", "><skipped /></testcase>"), 1), "no passing test belongs"),
        (suite(OK_CASE, 1, failures=2), "failures or errors"),  # counts that contradict cases
        (suite(OK_CASE, 1).replace('tests="1"', 'tests="one"'), "non-numeric"),
    ],
)
def test_bad_reports_are_rejected_even_with_a_zero_exit(
    tmp_path: Path, xml: str, problem: str
) -> None:
    tree = candidate_tree(tmp_path, "")
    res = gate.run_criterion(PYTEST, config.load(), tree, forging(xml))
    assert res["status"] == "fail"
    assert any(problem in p for p in res["report"]["problems"]), res["report"]


def test_a_well_formed_fresh_report_passes(tmp_path: Path) -> None:
    res = gate.run_criterion(PYTEST, config.load(), candidate_tree(tmp_path, ""), forging(
        suite(OK_CASE, 1)))  # fmt: skip
    assert res["status"] == "pass"


def test_a_report_never_rescues_a_non_zero_exit(tmp_path: Path) -> None:
    res = gate.run_criterion(
        PYTEST, config.load(), candidate_tree(tmp_path, ""), forging(suite(OK_CASE, 1), code=1)
    )
    assert res["status"] == "fail"


def test_a_candidate_supplied_report_is_never_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = candidate_tree(tmp_path, "")
    # reports the candidate shipped in its tree, valid in every way but freshness
    for guess in ("0" * 24, "deadbeef"):
        (tree / f".acp-junit-{guess}.xml").write_text(suite(OK_CASE, 1, name=guess))
    res = gate.run_criterion(PYTEST, config.load(), tree, lambda cmd, cwd: (0, "1 passed"))
    assert res["status"] == "fail"
    assert "no test report" in res["report"]["problems"][0]
    # and even a correctly guessed path is refused before anything runs
    monkeypatch.setattr("scripts.harness.gate.secrets.token_hex", lambda n: "0" * 24)
    ran: list[list[str]] = []

    def execute(cmd: list[str], cwd: Path) -> tuple[int, str]:
        ran.append(cmd)
        return 0, ""

    res = gate.run_criterion(PYTEST, config.load(), tree, execute)
    assert res["status"] == "fail" and ran == []


@pytest.mark.parametrize(
    ("target", "classname", "name", "matches"),
    [
        ("tests/test_x.py", "tests.test_x", "test_a", True),
        ("tests/test_x.py", "tests.test_x.TestCls", "test_a", True),
        ("tests/test_x.py::test_a", "tests.test_x", "test_a[1-2]", True),
        ("tests/test_x.py::test_a", "tests.test_x", "test_ab", False),
        ("tests/test_x.py::TestCls::test_a", "tests.test_x.TestCls", "test_a", True),
        ("tests/test_x.py", "tests.test_xy", "test_a", False),
    ],
)
def test_only_tests_from_the_requested_file_or_node_count(
    tmp_path: Path, target: str, classname: str, name: str, matches: bool
) -> None:
    report = tmp_path / "r.xml"
    report.write_text(suite(f'<testcase classname="{classname}" name="{name}" />', 1, name="n"))
    problems, counts = gate.junit_problems(report, "n", target)
    assert (counts["matched"] == 1) is matches
    assert (problems == []) is matches


# manual acceptance criteria


def test_manual_criteria_are_numbered_from_the_approved_contract() -> None:
    assert [mid for mid, _ in MANUAL] == ["M1", "M2", "M3"]
    assert MANUAL[0][1].startswith("expected values are literals")


def test_the_reviewer_cannot_silently_omit_a_manual_criterion() -> None:
    dropped = GOOD | {"manual_criteria": GOOD["manual_criteria"][:1]}
    assert pilot.review_problems(dropped, MANUAL) == [
        "reviewer omitted manual criterion M2",
        "reviewer omitted manual criterion M3",
    ]
    by_text = GOOD | {
        "manual_criteria": [
            {"criterion": text, "status": "met", "evidence": "x"} for _, text in MANUAL
        ]
    }
    assert pilot.review_problems(by_text, MANUAL) == []


def test_review_lists_the_contracts_manual_criteria_and_refuses_an_omission(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ev, expected, gh, run = review_setup(
        env, monkeypatch, GOOD | {"manual_criteria": GOOD["manual_criteria"][:2]}
    )
    out = env["tmp"] / "r"
    res = pilot.review(run, env["root"], env["cfg"], gh, CONTRACT, ev, expected, out)
    prompt = (out / "reviewer-prompt.md").read_text(encoding="utf-8")
    for mid, text in MANUAL:
        assert f"{mid}: {text}" in prompt
    assert res["accepted"] is False
    assert "reviewer omitted manual criterion M3" in res["problems"]


def test_the_verdict_lists_every_manual_criterion_even_if_the_review_omitted_one(
    tmp_path: Path,
) -> None:
    partial = GOOD | {"manual_criteria": GOOD["manual_criteria"][:2]}
    review = {"accepted": True, "problems": [], "verdict": partial,
              "engine_invoked": True, "engine_complete": True}  # fmt: skip
    ev, expected = evidence(tmp_path, **{"review.json": review})
    result = pilot.verdict(ev, expected)
    assert [m["id"] for m in result["manual_criteria_for_human"]] == ["M1", "M2", "M3"]
    assert {m["status"] for m in result["manual_criteria_for_human"]} == {"pending"}
    assert result["manual_criteria_for_human"][2]["reviewer"] == "omitted"
    assert result["ready_for_human"] is False
    assert "reviewer omitted manual criterion M3" in result["problems"]
