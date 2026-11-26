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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.harness import config, contract, gate, ledger, pilot, sandbox
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


def test_the_engine_gets_no_github_token_or_host_secrets(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", "ghs_" + "x" * 36)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_" + "y" * 36)
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-" + "z" * 30)  # not for the fake engine either
    monkeypatch.setenv("ACP_HOST_CANARY", CANARY)
    res, _, out = run_agent(
        env, monkeypatch, writes={"tests/test_demo_counts.py": "def test_x():\n    pass\n"}
    )
    seen = set(json.loads((out / "implementer-run.json").read_text())["env_names"])
    assert not seen & {
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "ACTIONS_RUNTIME_TOKEN",
        "ANTHROPIC_API_KEY",
        "ACP_HOST_CANARY",
    }
    assert res["status"] == "committed"


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

    def engine_then_plant(*args: Any, **kw: Any) -> int:
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
    writes = {"tests/test_demo_counts.py": "def test_x():\n    pass\n", **writes}
    res, _, out = run_agent(env, monkeypatch, writes=writes)
    report = check(env, res, out)
    assert report["ok"] is False


def test_a_secret_written_into_the_candidate_fails_the_check(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
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
        return 0, "ok"

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
    "manual_criteria": [{"criterion": "literals", "status": "unresolved", "evidence": "?"}],
    "suspected_injection": [],
}


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
    expected = {
        n: gate.sha256_file(ev / n)
        for n in ("agent.bundle", "gate-check.json", "diff.patch", "criteria.json")
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
    body = pilot.account(run_for("a" * 40), gh, tmp_path, "skipped")
    data = json.loads(body.split("```json\n")[1].split("\n```")[0])
    assert data["implementer_cost_usd"] is None
    assert data["reviewer_cost_usd"] == "not_run"


def evidence(tmp: Path, **override: dict[str, Any]) -> tuple[Path, dict[str, str]]:
    tip = "c" * 40
    files = {
        "agent.json": {"status": "committed", "tip": tip},
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
        "review.json": {"accepted": True, "problems": [], "verdict": GOOD},
    } | override
    ev = tmp / "ev"
    ev.mkdir(exist_ok=True)
    for name, data in files.items():
        (ev / name).write_text(json.dumps(data), encoding="utf-8")
    return ev, {
        n: gate.sha256_file(ev / n) for n in ("gate-check.json", "criteria.json", "review.json")
    }


def test_a_fully_verified_run_is_ready_for_a_human_and_lists_manual_criteria(
    tmp_path: Path,
) -> None:
    ev, expected = evidence(tmp_path)
    result = pilot.verdict(ev, expected)
    assert result["ready_for_human"] is True
    assert result["manual_criteria_for_human"] == GOOD["manual_criteria"]


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
