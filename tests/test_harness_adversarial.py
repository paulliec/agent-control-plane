"""Adversarial fixtures for the harness: what an agent might try, and whether
anything notices. Everything here is local and inert — temp git repos, one
pytest subprocess over a fixture written here, and payloads handed straight to
the guard. No network, no real agent, no agent-generated code.

Gap ids (G1-G5, S1) match docs/HARNESS.md. Each was pinned by a strict xfail
before its fix; the assertions are unchanged or stronger now that they pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from scripts.harness import config, contract, gate, guard, scope
from scripts.harness.config import REPO_ROOT

ALLOWED = ("tests/test_demo_counts.py",)
CONTRACT = "docs/harness/pilot-demo-counts.md"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    git(path, "config", "core.autocrlf", "false")
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A throwaway repo shaped like this one: base on main, agent branch checked out."""
    r = init_repo(tmp_path / "repo")
    (r / "tests").mkdir()
    (r / "docs/harness").mkdir(parents=True)
    (r / "scripts/harness").mkdir(parents=True)
    (r / "CLAUDE.md").write_text("rules\n")
    (r / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 96\n")
    (r / "tests/conftest.py").write_text("")
    (r / ".gitignore").write_text("*.db\n")
    (r / "scripts/harness/gate.py").write_text("# trusted\n")
    shutil.copy(REPO_ROOT / "harness.toml", r / "harness.toml")
    shutil.copy(REPO_ROOT / CONTRACT, r / CONTRACT)
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base")
    git(r, "checkout", "-qb", "agent/1-demo-counts")
    return r


def scoped(repo: Path, allowed: tuple[str, ...] = ALLOWED) -> scope.ScopeResult:
    return scope.check(scope.changed_files(repo, "main", "HEAD"), allowed, config.load())


def commit_all(repo: Path, msg: str = "change") -> None:
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", msg)


# --- protected files and out-of-scope changes, against real git ---------------


def test_a_committed_edit_to_a_protected_file_is_caught(repo: Path) -> None:
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    (repo / "pyproject.toml").write_text("[tool.coverage.report]\nfail_under = 0\n")
    commit_all(repo, "lower the floor")
    assert scoped(repo).protected == ("pyproject.toml",)


def test_deleting_a_protected_file_is_caught(repo: Path) -> None:
    git(repo, "rm", "-q", "CLAUDE.md")
    git(repo, "commit", "-qm", "rm")
    assert "CLAUDE.md" in scoped(repo).protected


def test_moving_a_protected_file_into_scope_is_caught(repo: Path) -> None:
    """S1: rename detection would list only the destination."""
    git(repo, "mv", "CLAUDE.md", "tests/test_demo_counts.py")
    git(repo, "commit", "-qm", "mv")
    assert "CLAUDE.md" in scoped(repo).protected


def test_only_commits_are_judged_so_leftovers_cannot_hide_or_count(repo: Path) -> None:
    # An interrupted run leaves staged, unstaged and untracked files behind.
    # The candidate travels as a bundle of commits, so none of it reaches the
    # verifier — and an empty commit set is not a pass.
    (repo / "tests/conftest.py").write_text("# staged\n")
    git(repo, "add", "tests/conftest.py")
    (repo / "CLAUDE.md").write_text("unstaged\n")
    (repo / "notes.txt").write_text("untracked\n")
    result = scoped(repo)
    assert result.changed == ()
    assert not result.ok


def test_a_force_added_ignored_file_is_caught(repo: Path) -> None:
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    (repo / "payload.db").write_text("anything\n")
    commit_all(repo)
    assert scoped(repo).changed == ("tests/test_demo_counts.py",)
    git(repo, "add", "-f", "payload.db")
    git(repo, "commit", "-qm", "sneak")
    assert "payload.db" in scoped(repo).outside_contract


# --- malicious test code -----------------------------------------------------

FAKE_PASS_CONFTEST = """
import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    report.outcome = "passed"
"""


def test_a_conftest_can_turn_a_failing_suite_green(tmp_path: Path) -> None:
    """Why conftest.py must not be grantable: it runs inside the gate."""
    (tmp_path / "conftest.py").write_text(FAKE_PASS_CONFTEST)
    (tmp_path / "test_x.py").write_text("def test_x():\n    assert False\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_")}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:cov"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0, proc.stdout


def test_a_contract_cannot_reach_conftest(repo: Path) -> None:
    """G3."""
    (repo / "tests/conftest.py").write_text(FAKE_PASS_CONFTEST)
    commit_all(repo, "helper")
    assert scoped(repo, ("tests/*",)).protected == ("tests/conftest.py",)


@pytest.mark.parametrize(
    "path",
    [
        "conftest.py",
        "tests/conftest.py",
        "tests/sub/conftest.py",
        "pytest.ini",
        "tests/tox.ini",
        "setup.cfg",
        "src/sitecustomize.py",
        "tests/x.pth",
    ],
)
def test_code_that_runs_before_the_tests_is_protected(path: str) -> None:
    assert config.load().is_protected(path)
    assert decide("Write", file_path=path) is not None


# --- reading secrets and host files -----------------------------------------


def decide(tool: str, **tool_input: Any) -> str | None:
    return guard.decide(tool, tool_input, config.load(), REPO_ROOT, "agent/1-demo-counts")


def test_reading_host_files_or_secrets_is_blocked() -> None:
    home = Path.home()
    assert decide("Read", file_path=str(home / ".ssh/id_ed25519")) is not None
    assert decide("Read", file_path=str(home / ".claude/settings.json")) is not None
    assert decide("Read", file_path="../agent control plane/CLAUDE.md") is not None
    assert decide("Read", file_path=".env.production") is not None


@pytest.mark.parametrize("tool", ["Grep", "Glob"])
def test_searching_outside_the_repo_is_blocked(tool: str) -> None:
    """G2."""
    assert decide(tool, pattern="*", path=str(Path.home())) is not None
    assert decide(tool, pattern="*", path="..") is not None
    assert decide(tool, pattern="*", path="../agent control plane") is not None


@pytest.mark.parametrize(
    "pattern", ["C:/Users/**/*.pem", "/etc/*", "~/.ssh/*", "../**/*.env", "tests/../../*"]
)
def test_a_glob_pattern_cannot_point_outside(pattern: str) -> None:
    assert decide("Glob", pattern=pattern) is not None


def test_searching_inside_the_repo_is_allowed() -> None:
    assert decide("Grep", pattern="tenant_id", path="src") is None
    assert decide("Grep", pattern="tenant_id") is None
    assert decide("Glob", pattern="tests/**/*.py") is None


def test_unattended_read_permission_is_scoped_to_the_repo() -> None:
    """G2: no unscoped read-family allow rule."""
    s = json.loads((REPO_ROOT / ".claude/harness/settings.json").read_text(encoding="utf-8"))
    allow = s["permissions"]["allow"]
    assert "Read" not in allow
    assert not {"Grep", "Glob"} & set(allow)
    assert "Read(./**)" in allow
    assert {"Grep", "Glob"} <= set(s["hooks"]["PreToolUse"][0]["matcher"].split("|"))


# --- shell evasions ----------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git -C . push",
        "git -c core.pager=cat push",
        "git log -1 --output=CLAUDE.md",
        "git diff --output=../outside.txt",
    ],
)
def test_git_evasions_are_blocked(command: str) -> None:
    """G4."""
    assert decide("Bash", command=command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "/usr/bin/git push",
        "env git push",
        "git --git-dir=../other/.git status",
        "git --work-tree=.. add -A",
        "git commit -n -m x",
        "git show --output CLAUDE.md HEAD",
        "git stash",
        "git restore CLAUDE.md",
        "git apply patch.diff",
        "git fetch origin",
        "git clone https://example.com/x",
        "git status; git push",
        "bash -c 'git push'",
        'sh -c "curl example.com"',
        "eval git push",
        "git log 'unterminated",
    ],
)
def test_more_git_and_shell_evasions_are_blocked(command: str) -> None:
    assert decide("Bash", command=command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git --no-pager log -5 --oneline",
        "git diff main...HEAD -- tests/",
        "git show HEAD:tests/conftest.py",
        "git add tests/test_demo_counts.py && git commit -m 'add demo count test'",
        "bash scripts/verify.sh",
    ],
)
def test_ordinary_git_still_works(command: str) -> None:
    assert decide("Bash", command=command) is None


def test_git_output_really_writes_files(repo: Path) -> None:
    """The concrete reason G4 matters — and why commits are checked regardless."""
    git(repo, "log", "-1", "--output=CLAUDE.md")
    assert (repo / "CLAUDE.md").read_text().startswith("commit ")
    commit_all(repo)
    assert "CLAUDE.md" in scoped(repo).protected


# --- the trusted verifier (G1, G5) -------------------------------------------


def trusted(repo: Path) -> str:
    """Put the repo back on its pinned base, as the verifier's checkout would be."""
    git(repo, "checkout", "-q", "main")
    return git(repo, "rev-parse", "HEAD")


def test_the_verifier_passes_a_clean_in_scope_candidate(repo: Path) -> None:
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo)
    base = trusted(repo)
    report = gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT)
    assert report["ok"] is True
    assert report["criteria_run"] is False
    assert {c["status"] for c in report["criteria"]} == {"not_run"}


def test_a_candidate_that_rewrites_the_gate_is_judged_by_the_base(repo: Path) -> None:
    """G1: the candidate's gate.py, harness.toml and contract are never consulted."""
    (repo / "scripts/harness/gate.py").write_text("print('ok')\n")
    toml = repo / "harness.toml"
    toml.write_text(toml.read_text().replace('"CLAUDE.md",', ""))
    (repo / CONTRACT).write_text(
        (repo / CONTRACT)
        .read_text()
        .replace("- tests/test_demo_counts.py", "- tests/test_demo_counts.py\n- CLAUDE.md")
    )
    (repo / "CLAUDE.md").write_text("weakened\n")
    commit_all(repo)
    base = trusted(repo)
    report = gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT)
    assert report["ok"] is False
    assert set(report["scope"]["protected"]) == {
        "CLAUDE.md",
        "harness.toml",
        CONTRACT,
        "scripts/harness/gate.py",
    }


def test_the_contract_comes_from_the_base_commit_not_the_working_file(repo: Path) -> None:
    base = trusted(repo)
    git(repo, "checkout", "-q", "agent/1-demo-counts")
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo)
    git(repo, "checkout", "-q", "main")
    # the trusted checkout must be clean; an edited contract on disk is refused
    (repo / CONTRACT).write_text("### Goal\n\nanything\n")
    with pytest.raises(gate.GateError, match="local changes"):
        gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT)


def test_the_verifier_refuses_an_unpinned_or_wrong_base(repo: Path) -> None:
    git(repo, "checkout", "-q", "main")
    with pytest.raises(gate.GateError, match="full commit SHA"):
        gate.evaluate(repo, "main", "agent/1-demo-counts", CONTRACT)
    with pytest.raises(gate.GateError, match="pinned base"):
        gate.evaluate(repo, "0" * 40, "agent/1-demo-counts", CONTRACT)


def test_the_verifier_refuses_a_non_agent_ref(repo: Path) -> None:
    git(repo, "branch", "feature/x")
    base = trusted(repo)
    with pytest.raises(gate.GateError, match="agent/"):
        gate.evaluate(repo, base, "feature/x", CONTRACT)


def test_a_candidate_with_rewritten_history_is_rejected(repo: Path) -> None:
    """G5: an orphan history diffs clean from the merge-base but is not built on the base."""
    base = trusted(repo)
    git(repo, "checkout", "-q", "--orphan", "agent/2-orphan")
    git(repo, "rm", "-rq", "--cached", ".")
    for f in repo.iterdir():
        if f.name != ".git":
            shutil.rmtree(f) if f.is_dir() else f.unlink()
    (repo / "tests").mkdir()
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo, "orphan")
    git(repo, "checkout", "-q", "-f", "main")
    report = gate.evaluate(repo, base, "agent/2-orphan", CONTRACT)
    assert report["ok"] is False
    assert "descend" in report["error"]


def test_the_verifier_imports_only_an_agent_head_from_a_bundle(repo: Path, tmp_path: Path) -> None:
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo)
    git(repo, "branch", "evil/main")
    good = tmp_path / "agent.bundle"
    bad = tmp_path / "evil.bundle"
    git(repo, "bundle", "create", str(good), "main..agent/1-demo-counts")
    git(repo, "bundle", "create", str(bad), "main..evil/main")
    base = trusted(repo)
    git(repo, "branch", "-D", "agent/1-demo-counts", "evil/main")

    report = gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT, bundle=good)
    assert report["ok"] is True
    # quarantined outside refs/heads, and the trusted checkout stays on its base
    assert git(repo, "branch", "--list", "agent/*") == ""
    assert git(repo, "rev-parse", "HEAD") == base

    with pytest.raises(gate.GateError, match="exactly"):
        gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT, bundle=bad)


def test_candidate_code_is_never_run_outside_a_sandbox(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "tests/test_demo_counts.py").write_text("raise SystemExit('ran on the host')\n")
    commit_all(repo)
    base = trusted(repo)
    monkeypatch.delenv("ACP_SANDBOX", raising=False)

    def must_not_run(*_: object) -> None:
        raise AssertionError("candidate code executed")

    monkeypatch.setattr(gate, "run_criterion", must_not_run)
    with pytest.raises(gate.GateError, match="sandbox"):
        gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT, run_criteria=True)
    # and on Windows the flag alone is not enough
    monkeypatch.setenv("ACP_SANDBOX", "1")
    if os.name == "nt":
        with pytest.raises(gate.GateError, match="sandbox"):
            gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT, run_criteria=True)


def test_the_gate_runs_no_criteria_over_a_disqualified_diff(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    (repo / "pyproject.toml").write_text("")
    commit_all(repo)
    base = trusted(repo)
    monkeypatch.setattr(gate, "sandboxed", lambda: True)

    def must_not_run(*_: object) -> None:
        raise AssertionError("criteria ran over a protected-path diff")

    monkeypatch.setattr(gate, "run_criterion", must_not_run)
    report = gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT, run_criteria=True)
    assert report["ok"] is False
    assert report["scope"]["protected"] == ("pyproject.toml",)


def test_the_gate_cli_reports_errors_as_a_failed_verdict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gate.main(["--base", "main", "--ref", "agent/x", "--contract", CONTRACT]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] is False
    assert "full commit SHA" in report["error"]


# --- can the implementer or reviewer weaken the gate? -------------------------


@pytest.mark.parametrize(
    "path",
    [
        CONTRACT,
        "scripts/harness/gate.py",
        "scripts/verify.sh",
        "harness.toml",
        "pyproject.toml",
        ".claude/agents/reviewer.md",
        ".claude/harness/settings.json",
        ".github/workflows/ci.yml",
        "config/agents.yaml",
        "data/samples/mcp_tool_calls.jsonl",
    ],
)
def test_everything_that_defines_the_gate_is_protected(path: str) -> None:
    assert config.load().is_protected(path)


def test_a_manual_criterion_never_counts_as_passed() -> None:
    result = gate.run_criterion(
        contract.Criterion("manual", "looks right"), config.load(), REPO_ROOT
    )
    assert result["status"] == "pending"


def test_the_reviewer_cannot_be_handed_write_tools() -> None:
    text = (REPO_ROOT / ".claude/agents/reviewer.md").read_text(encoding="utf-8")
    assert "never the implementer's summary" in text
    assert "tools: Read, Grep, Glob\n" in text


# --- PR #1 review findings ----------------------------------------------------

FENCE_ONLY = "```\n```"


def pilot_with_criteria(criteria: str) -> str:
    text = (REPO_ROOT / CONTRACT).read_text(encoding="utf-8")
    head, rest = text.split("### Acceptance criteria", 1)
    tail = rest[rest.index("### Stop conditions") :]
    return f"{head}### Acceptance criteria\n\n{criteria}\n\n{tail}"


def test_a_fence_only_criteria_field_is_rejected() -> None:
    """P1: an empty code fence normalised to zero criteria and skipped the verify rule."""
    with pytest.raises(contract.ContractError, match="verify"):
        contract.parse(pilot_with_criteria(FENCE_ONLY), config.load())


def test_a_fence_only_contract_at_the_base_is_refused_by_the_gate(repo: Path) -> None:
    # the approved (base) contract itself is the malformed one
    git(repo, "checkout", "-q", "main")
    (repo / CONTRACT).write_text(pilot_with_criteria(FENCE_ONLY), encoding="utf-8")
    git(repo, "commit", "-qam", "approved contract, fence-only criteria")
    git(repo, "checkout", "-q", "agent/1-demo-counts")
    git(repo, "merge", "-q", "--ff-only", "main")
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo)
    base = trusted(repo)
    with pytest.raises(contract.ContractError, match="verify"):
        gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT, run_criteria=True)


def test_the_gate_never_accepts_a_run_without_a_passing_verify(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defence in depth: even a contract that slipped past the parser can't pass."""
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo)
    base = trusted(repo)
    real_parse = contract.parse

    def no_criteria(body: str, cfg: config.Config) -> contract.Contract:
        c = real_parse(body, cfg)
        return contract.Contract(
            c.goal, c.approved_item, c.allowed_paths, (), c.stop_conditions, c.out_of_scope
        )

    monkeypatch.setattr(contract, "parse", no_criteria)
    monkeypatch.setattr(gate, "sandboxed", lambda: True)
    report = gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT, run_criteria=True)
    assert report["ok"] is False


def test_a_protected_edit_reverted_later_is_still_caught(repo: Path) -> None:
    """P2: the endpoint diff is clean, but the history carries the protected edit."""
    (repo / "CLAUDE.md").write_text("weakened\n")
    commit_all(repo, "edit protected")
    (repo / "CLAUDE.md").write_text("rules\n")
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo, "restore and add test")
    assert "CLAUDE.md" in scoped(repo).protected
    base = trusted(repo)
    report = gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT)
    assert report["ok"] is False
    assert "CLAUDE.md" in report["scope"]["protected"]


def test_an_out_of_scope_file_added_then_deleted_is_still_caught(repo: Path) -> None:
    (repo / "notes.txt").write_text("exfil\n")
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo, "add")
    git(repo, "rm", "-q", "notes.txt")
    git(repo, "commit", "-qm", "delete")
    assert "notes.txt" in scoped(repo).outside_contract


def test_a_candidate_containing_a_merge_commit_is_rejected(repo: Path) -> None:
    """Merge parents would need per-parent diffs; agents can't merge, so refuse them."""
    git(repo, "checkout", "-qb", "side", "main")
    (repo / "CLAUDE.md").write_text("from the side\n")
    commit_all(repo, "side")
    git(repo, "checkout", "-q", "agent/1-demo-counts")
    (repo / "tests/test_demo_counts.py").write_text("def test_x(): pass\n")
    commit_all(repo)
    git(repo, "merge", "-q", "--no-ff", "-m", "merge", "side")
    base = trusted(repo)
    report = gate.evaluate(repo, base, "agent/1-demo-counts", CONTRACT)
    assert report["ok"] is False
    assert "merge" in report["error"]


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Grep", {"pattern": ".*", "path": ".env.production"}),
        ("Grep", {"pattern": ".*", "path": "config/.env.local"}),
        ("Grep", {"pattern": ".*", "glob": ".env*"}),
        ("Grep", {"pattern": ".*", "glob": "**/.env.*"}),
        ("Grep", {"pattern": ".*", "path": "config", "glob": "*.env"}),
        ("Glob", {"pattern": ".env*"}),
        ("Glob", {"pattern": "**/.env.*"}),
        ("Glob", {"pattern": "config/.env.local"}),
        ("Glob", {"pattern": "*", "path": "config/.env.d"}),
    ],
)
def test_searches_cannot_target_secret_files(tool: str, tool_input: dict[str, Any]) -> None:
    """P2: the search branch returned before the Read branch's .env check."""
    assert decide(tool, **tool_input) is not None


def test_ordinary_searches_still_work_next_to_the_secret_rule() -> None:
    assert decide("Grep", pattern="environment", path="src", glob="*.py") is None
    assert decide("Glob", pattern="src/**/environment*.py") is None


def test_secret_files_are_denied_at_any_depth_in_permissions() -> None:
    s = json.loads((REPO_ROOT / ".claude/harness/settings.json").read_text(encoding="utf-8"))
    assert {"Read(./.env*)", "Read(./**/.env*)", "Read(./**/*.env)"} <= set(
        s["permissions"]["deny"]
    )
