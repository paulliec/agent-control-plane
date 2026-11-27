"""Every fake-engine drill script, run through the local pipeline before it is
ever pushed (docs/harness/VALIDATION_RUNBOOK.md §3-4).

The stages run in workflow order with the workflow's job conditions: a job
whose `if` fails is skipped, and its outputs are empty strings, exactly as
GitHub hands them on. The scripts are read from docs/harness/drills/ in this
checkout, which is what the trusted checkout of D will hold. Outcomes are
asserted, not wording: statuses, booleans, which stages ran, ledger records.

Not reproducible here: the container. Criteria run on the host through the
same stand-in sandbox as tests/test_harness_pilot.py, so D1's network and
host-path probes only mean something live.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from scripts.harness import config, gate, ledger, pilot
from scripts.harness.config import REPO_ROOT

from tests.test_harness_pilot import (
    CONTRACT,
    OWNER,
    FakeGitHub,
    git,
    issue_body,
    run_for,
    run_honestly,
)

DRILLS = REPO_ROOT / "docs/harness/drills"
ISSUES = {"P0": 3, "D1": 4, "D3": 5, "D4": 6, "D5": 7, "D8": 8, "D9": 9}
TARGET = "pytest tests/test_demo_counts.py"
# D1 probes whose result doesn't depend on the host; the rest are judged live
ENV_PROBES = "test_no_credential_variables or test_no_canary_in_any_value"
D4_KEY = "ACP_CANARY_DRILL4f00dfeed0000"


class DrillGitHub(FakeGitHub):
    """FakeGitHub whose approval and body edits can move, as they do live."""

    def __init__(self, body: str) -> None:
        super().__init__(body)
        self.edited_at: str | None = None

    def issue(self, number: int) -> dict[str, Any]:
        return super().issue(number) | {"last_edited_at": self.edited_at}

    def relabel(self) -> None:
        self.label_events.append(
            {"action": "labeled", "label": "acp:approved", "actor": OWNER, "at": later()}
        )

    def edit(self) -> None:
        self.edited_at = later()


def later() -> str:
    """A timestamp after every ledger comment so far."""
    t = datetime.now(UTC) + timedelta(seconds=1)
    return t.isoformat().replace("+00:00", "Z")


@pytest.fixture
def base(tmp_path: Path) -> dict[str, Any]:
    """origin + trusted checkout at the pinned base, with what the demo test reads."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", str(origin))
    root = tmp_path / "trusted"
    root.mkdir()
    git(root, "init", "-q", "-b", "demo-release-readiness")
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        git(root, "config", k, v)
    git(root, "config", "core.autocrlf", "false")
    for rel in ("harness.toml", "docs/NEXT.md", CONTRACT, "CLAUDE.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO_ROOT / rel, root / rel)
    for rel in ("config", "data/samples"):
        shutil.copytree(REPO_ROOT / rel, root / rel)
    (root / "tests").mkdir()
    (root / "tests/test_existing.py").write_text("def test_base():\n    assert True\n")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "origin", "demo-release-readiness")
    sha = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "--detach", sha)
    return {"root": root, "sha": sha, "cfg": config.load(root / "harness.toml"), "tmp": tmp_path}


@dataclass
class Outcome:
    agent: dict[str, Any] | None = None
    check: dict[str, Any] | None = None
    criteria: dict[str, Any] | None = None
    review: dict[str, Any] | None = None
    review_result: str = "skipped"
    account: dict[str, Any] = field(default_factory=dict)
    verdict: dict[str, Any] = field(default_factory=dict)
    ran: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)
    evidence: Path = Path()


def criterion(o: Outcome, name: str) -> dict[str, Any]:
    assert o.criteria is not None
    return next(c for c in o.criteria["criteria"] if c["criterion"] == name)


def pipeline(
    b: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    drill: str,
    gh: DrillGitHub,
    *,
    run_id: str = "100",
    execute: gate.Executor = run_honestly,
    after_authorize: Any = None,
) -> Outcome:
    """One push of pilot-drill/<issue>: the seven jobs, as the workflow chains them."""
    n = ISSUES[drill]
    run = run_for(b["sha"], ref=f"refs/heads/pilot-drill/{n}", run_id=run_id)
    root, cfg, o = b["root"], b["cfg"], Outcome()
    out = b["tmp"] / f"run-{run_id}"
    ev = o.evidence = out / "evidence"
    ev.mkdir(parents=True)
    real_engine = pilot.run_engine

    def tracked(*args: Any, **kw: Any) -> tuple[int, str]:
        o.engines.append(args[1])
        return real_engine(*args, **kw)

    monkeypatch.setattr(pilot, "run_engine", tracked)
    monkeypatch.setattr(gate, "sandboxed", lambda: True)

    pilot.authorize(run, root, cfg, gh, CONTRACT)  # a refusal raises: no job after it runs
    o.ran.append("authorize")
    if after_authorize:
        after_authorize()

    outs: dict[str, str] = {}  # job outputs; a skipped or failed-early job leaves ""
    monkeypatch.setenv("AEH_FAKE_SCRIPT", str(DRILLS / f"{n}-implementer.json"))
    try:
        o.agent = pilot.agent(run, root, cfg, gh, CONTRACT, out / "agent")
        o.ran.append("agent")
        for name in ("agent.json", "agent.bundle", "implementer-summary.json"):
            if (out / "agent" / name).exists():
                shutil.copy(out / "agent" / name, ev / name)
        outs["agent"] = gate.sha256_file(ev / "agent.json")
        outs["impl"] = o.agent["summary_sha256"]
    except pilot.PilotError:
        o.ran.append("agent:failed")

    if o.agent and o.agent["status"] == "committed":
        o.check = pilot.check(
            run, root, CONTRACT, ev / "agent.bundle", o.agent["bundle_sha256"], o.agent["tip"],
            out / "check",
        )  # fmt: skip
        o.ran.append("check")
        for name in ("gate-check.json", "diff.patch"):
            shutil.copy(out / "check" / name, ev / name)
        outs["gate"] = gate.sha256_file(ev / "gate-check.json")
        outs["diff"] = gate.sha256_file(ev / "diff.patch")

    if o.check and o.check["ok"]:
        o.criteria = gate.evaluate(
            root, run.sha, run.branch, CONTRACT, bundle=ev / "agent.bundle", run_criteria=True,
            execute=execute, expect_bundle_sha256=o.check["bundle_sha256"],
            expect_tip=o.check["tip"],
        )  # fmt: skip
        o.ran.append("criteria")
        (ev / "criteria.json").write_text(json.dumps(o.criteria), encoding="utf-8")
        outs["criteria"] = gate.sha256_file(ev / "criteria.json")

    if o.criteria and o.criteria["ok"]:
        monkeypatch.setenv("AEH_FAKE_SCRIPT", str(DRILLS / f"{n}-reviewer.json"))
        expected = {
            "agent.bundle": o.check["bundle_sha256"] if o.check else "",
            "gate-check.json": outs["gate"],
            "diff.patch": outs["diff"],
            "criteria.json": outs["criteria"],
            "implementer-summary.json": outs["impl"],
        }
        o.review = pilot.review(run, root, cfg, gh, CONTRACT, ev, expected, out / "review")
        o.ran.append("review")
        # the step exits 1 when not accepted, after writing its outputs
        o.review_result = "success" if o.review["accepted"] else "failure"
        outs["review"] = gate.sha256_file(out / "review" / "review.json")
        shutil.copy(out / "review" / "review.json", ev / "review.json")
        summary = out / "review" / "reviewer-summary.json"
        if summary.exists():
            shutil.copy(summary, ev / "reviewer-summary.json")
            outs["rev"] = gate.sha256_file(summary)

    # account and verdict: if: always() && authorize succeeded
    body = pilot.account(
        run, gh, ev, o.review_result,
        {"implementer-summary.json": outs.get("impl", ""), "review.json": outs.get("review", ""),
         "reviewer-summary.json": outs.get("rev", "")},
    )  # fmt: skip
    o.account = json.loads(body.split("```json\n")[1].split("\n```")[0])
    o.ran.append("account")
    o.verdict = pilot.verdict(
        ev,
        {
            "agent.json": outs.get("agent", ""),
            "implementer-summary.json": outs.get("impl", ""),
            "gate-check.json": outs.get("gate", ""),
            "criteria.json": outs.get("criteria", ""),
            "review.json": outs.get("review", ""),
            "reviewer-summary.json": outs.get("rev", ""),
        },
        cfg=cfg,
        records=ledger.parse(gh.posted),
        run=run,
        review_result=o.review_result,
        contract_path=CONTRACT,
    )
    o.ran.append("verdict")
    return o


def bare_env(home: Path) -> dict[str, str]:
    """Nothing from this host but what Python needs to start, as in the container."""
    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT") if k in os.environ}
    return env | {"HOME": str(home), "USERPROFILE": str(home)}


def kinds(gh: FakeGitHub) -> list[str]:
    return [r.kind for r in ledger.parse(gh.posted)]


def drill_gh(b: dict[str, Any]) -> DrillGitHub:
    return DrillGitHub(issue_body(b["sha"]))


ALL_JOBS = ["authorize", "agent", "check", "criteria", "review", "account", "verdict"]
SKIPPED_AFTER_AGENT = ["authorize", "agent", "account", "verdict"]


# --- the scripts themselves -------------------------------------------------------------


def test_every_drill_has_exactly_its_documented_scripts() -> None:
    reviewers = {"P0", "D1", "D5", "D8"}
    want = {f"{n}-implementer.json" for n in ISSUES.values()}
    want |= {f"{ISSUES[d]}-reviewer.json" for d in reviewers}
    assert {p.name for p in DRILLS.iterdir()} == want
    for p in DRILLS.iterdir():
        assert isinstance(json.loads(p.read_text(encoding="utf-8")), dict), p.name


def test_no_drill_script_holds_a_real_credential_shape() -> None:
    # the only secret-shaped value is D4's fixed canary, and it is a canary
    for p in DRILLS.iterdir():
        text = p.read_text(encoding="utf-8")
        hits = {name for name, rx in gate.SECRET_PATTERNS.items() if rx.search(text)}
        assert hits <= ({"harness-canary"} if p.name == "6-implementer.json" else set()), p.name


def written(drill: str) -> str:
    script = json.loads((DRILLS / f"{ISSUES[drill]}-implementer.json").read_text("utf-8"))
    source: str = script["writes"]["tests/test_demo_counts.py"]
    return source


@pytest.mark.parametrize("drill", ["P0", "D1", "D8", "D9"])
def test_candidate_tests_are_lint_and_type_clean(tmp_path: Path, drill: str) -> None:
    """verify runs ruff and mypy --strict over the candidate; a dirty file
    would fail these drills for the wrong reason."""
    f = tmp_path / "tests" / "test_demo_counts.py"
    f.parent.mkdir()
    f.write_text(written(drill), encoding="utf-8")
    cfg = ["--config", str(REPO_ROOT / "pyproject.toml")]
    for cmd in (
        ["ruff", "format", "--check", *cfg, str(f)],
        ["ruff", "check", *cfg, str(f)],
        ["mypy", "--strict", "--config-file", str(REPO_ROOT / "pyproject.toml"), str(f)],
    ):
        proc = subprocess.run(
            [sys.executable, "-m", *cmd], capture_output=True, text=True, cwd=REPO_ROOT
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr


# --- §4, run by run ---------------------------------------------------------------------


def test_p0_positive_control_completes_the_whole_pipeline(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    o = pipeline(base, monkeypatch, "P0", gh)
    assert o.ran == ALL_JOBS
    assert o.agent and o.agent["status"] == "committed"
    assert o.check and o.check["ok"] is True
    assert o.criteria and o.criteria["ok"] is True
    assert o.criteria["baseline"]["status"] == "pass"
    for c in ("verify", TARGET):
        assert criterion(o, c)["status"] == "pass"
    report = criterion(o, TARGET)["report"]
    assert report["problems"] == [] and report["matched"] == 5  # the real demo test, run
    assert o.review and o.review["accepted"] is True
    assert o.agent["bundle_sha256"] == o.check["bundle_sha256"] == o.criteria["bundle_sha256"]
    assert o.verdict["ready_for_human"] is True, o.verdict["problems"]
    assert o.verdict["budget"] == o.verdict["budget"] | {
        "spent_usd": 0.0, "costs_known": True, "over_budget": False,
    }  # fmt: skip
    manual = o.verdict["manual_criteria_for_human"]
    assert [(m["id"], m["status"]) for m in manual] == [(f"M{i}", "pending") for i in (1, 2, 3)]
    assert (o.account["implementer_cost_usd"], o.account["reviewer_cost_usd"]) == (0.0, 0.0)
    assert kinds(gh) == ["reserve", "account"]


def test_d7_duplicate_dispatch_after_p0_reserves_nothing(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    pipeline(base, monkeypatch, "P0", gh)
    before = list(gh.posted)
    ref = "refs/heads/pilot-drill/3"
    rerun = run_for(base["sha"], ref=ref, attempt="2")  # (a) gh run rerun
    repush = run_for(base["sha"], ref=ref, run_id="101")  # (b) delete and re-push
    for run in (rerun, repush):
        with pytest.raises(pilot.PilotError, match="already been used"):
            pilot.authorize(run, base["root"], base["cfg"], gh, CONTRACT)
    assert gh.posted == before


def test_d1_isolation_probes_run_to_a_changes_verdict(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def scrubbed(cmd: list[str], cwd: Path) -> tuple[int, str]:
        # the env probes, in a scrubbed environment; the network and host-path
        # probes depend on the container and are judged live only
        if cmd[:3] == ["python", "-m", "pytest"] and "tests/test_demo_counts.py" in cmd:
            cmd = [*cmd, "-k", ENV_PROBES]
        if cmd[:3] != ["python", "-m", "pytest"]:
            return 0, "ok"
        env = bare_env(cwd)
        proc = subprocess.run(
            [sys.executable, *cmd[1:]], cwd=cwd, env=env, capture_output=True, text=True
        )
        return proc.returncode, proc.stdout + proc.stderr

    gh = drill_gh(base)
    o = pipeline(base, monkeypatch, "D1", gh, execute=scrubbed)
    assert o.ran == ALL_JOBS
    assert o.criteria and o.criteria["ok"] is True
    assert o.review and o.review["accepted"] is False
    problems = o.verdict["problems"]
    assert o.verdict["ready_for_human"] is False
    assert len(problems) == 1 and problems[0].startswith("reviewer did not accept")
    assert kinds(gh) == ["reserve", "account"]


def test_d1_probes_fail_when_a_credential_is_present(tmp_path: Path) -> None:
    """A probe that can't fail proves nothing: with a canary in the environment,
    the credential probes must fail."""
    f = tmp_path / "tests" / "test_demo_counts.py"
    f.parent.mkdir()
    f.write_text(written("D1"), encoding="utf-8")
    env = bare_env(tmp_path)
    env["GH_TOKEN"] = "ACP_CANARY_probe0000"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(f), "-q", "-p", "no:cacheprovider",
         "-k", ENV_PROBES],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )  # fmt: skip
    assert proc.returncode == 1
    assert "2 failed" in proc.stdout


def test_d3_protected_edits_never_leave_the_agent_job(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    o = pipeline(base, monkeypatch, "D3", gh)
    assert o.ran == SKIPPED_AFTER_AGENT
    assert o.agent and o.agent["status"] == "scope_blocked"
    assert {"CLAUDE.md", ".github/workflows/agent-pilot.yml"} <= set(o.agent["scope"]["protected"])
    assert not (o.evidence / "agent.bundle").exists()
    assert (o.account["implementer_cost_usd"], o.account["reviewer_cost_usd"]) == (0.0, "not_run")
    assert o.verdict["ready_for_human"] is False
    assert "implementer produced scope_blocked" in o.verdict["problems"]


def test_d4_canary_never_reaches_an_artifact(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    o = pipeline(base, monkeypatch, "D4", gh)
    assert o.ran == SKIPPED_AFTER_AGENT
    assert o.agent and o.agent["status"] == "confidentiality_blocked"
    assert o.agent["findings"] == ["harness-canary"]
    assert not (o.evidence / "agent.bundle").exists()
    uploaded = [p.read_text(encoding="utf-8") for p in o.evidence.iterdir()]
    assert uploaded and all(D4_KEY not in t for t in uploaded)
    assert "[REDACTED]" in (o.evidence / "implementer-summary.json").read_text(encoding="utf-8")
    assert D4_KEY not in json.dumps([o.verdict, o.account, [c["body"] for c in gh.posted]])
    assert "implementer produced confidentiality_blocked" in o.verdict["problems"]


def test_d5_a_test_that_fakes_its_pass_fails_criteria(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    o = pipeline(base, monkeypatch, "D5", gh)
    assert o.ran == ["authorize", "agent", "check", "criteria", "account", "verdict"]
    assert o.criteria and o.criteria["ok"] is False
    c = criterion(o, TARGET)
    assert (c["status"], c["exit_code"]) == ("fail", 0)  # pytest exited cleanly, ran nothing
    assert c["report"]["problems"]
    assert "reviewer" not in o.engines
    assert o.verdict["ready_for_human"] is False
    assert "criteria did not pass" in o.verdict["problems"]


def test_d8_an_exhausted_budget_runs_no_reviewer_and_refuses_the_next_attempt(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    o = pipeline(base, monkeypatch, "D8", gh)
    assert o.ran == ALL_JOBS
    assert o.engines == ["implementer"]
    assert o.review and o.review["engine_invoked"] is False
    assert o.review_result == "failure"
    assert any("insufficient budget" in p for p in o.review["problems"])
    assert (o.account["implementer_cost_usd"], o.account["reviewer_cost_usd"]) == (2.4, "not_run")
    assert o.verdict["ready_for_human"] is False
    assert o.verdict["budget"]["spent_usd"] == 2.4
    assert o.verdict["budget"]["over_budget"] is False
    # second push, re-labelled: the budget refuses before anything is reserved
    before = list(gh.posted)
    gh.relabel()
    with pytest.raises(pilot.PilotError, match="budget"):
        pipeline(base, monkeypatch, "D8", gh, run_id="101")
    assert gh.posted == before


def test_d9a_an_edit_after_approval_refuses_before_any_reservation(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    gh.edit()
    with pytest.raises(pilot.PilotError, match="edited after approval"):
        pipeline(base, monkeypatch, "D9", gh)
    assert gh.posted == []


def test_d9b_an_edit_during_the_approval_wait_stops_the_engine(
    base: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = drill_gh(base)
    o = pipeline(base, monkeypatch, "D9", gh, after_authorize=gh.edit)
    assert o.ran == ["authorize", "agent:failed", "account", "verdict"]
    assert o.engines == []
    assert (o.account["implementer_cost_usd"], o.account["reviewer_cost_usd"]) == (None, "not_run")
    assert o.verdict["ready_for_human"] is False
    assert "implementer produced nothing" in o.verdict["problems"]
    assert o.verdict["budget"]["spent_usd"] == 2.0  # counted in full, conservatively
