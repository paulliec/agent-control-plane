"""The development harness (scripts/harness/). Not product code, but it is the
thing standing between an unattended agent and the repo, so its refusals are
tested as carefully as its happy path.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from scripts.harness import config, contract, guard, scope
from scripts.harness.config import REPO_ROOT, Config

PILOT = REPO_ROOT / "docs/harness/pilot-demo-counts.md"
NEXT_MD = (REPO_ROOT / "docs/NEXT.md").read_text(encoding="utf-8")
OWNER = "paulliec"


@pytest.fixture(scope="module")
def cfg() -> Config:
    return config.load()


def body(**overrides: str) -> str:
    sections = {
        "Goal": "Pin the demo numbers.",
        "Approved work item": "1. Public demo readiness / deployment",
        "Allowed paths": "- tests/test_demo_counts.py",
        "Acceptance criteria": "- verify\n- pytest tests/test_demo_counts.py",
        "Stop conditions": "Numbers disagree with the docs.",
    }
    sections.update(overrides)
    return "\n\n".join(f"### {k}\n\n{v}" for k, v in sections.items())


# --- contract parsing -------------------------------------------------------


def test_the_pilot_contract_is_valid_against_the_real_config(cfg: Config) -> None:
    c = contract.parse(PILOT.read_text(encoding="utf-8"), cfg)
    assert c.allowed_paths == ("tests/test_demo_counts.py",)
    assert [k.kind for k in c.criteria][:2] == ["verify", "pytest"]
    assert c.approved_item in contract.approved_items(NEXT_MD)


def test_approved_items_are_read_from_next_md() -> None:
    items = contract.approved_items(NEXT_MD)
    assert items[0] == "1. Public demo readiness / deployment"
    assert any(i.startswith("3. Development harness") for i in items)
    # nothing from the "Not approved yet" list leaks in
    assert not any("Tollgate 3" in i for i in items)


def test_a_missing_required_section_is_reported(cfg: Config) -> None:
    text = body().replace("### Stop conditions\n\nNumbers disagree with the docs.", "")
    with pytest.raises(contract.ContractError, match="stop_conditions"):
        contract.parse(text, cfg)


def test_github_no_response_counts_as_missing(cfg: Config) -> None:
    with pytest.raises(contract.ContractError, match="goal"):
        contract.parse(body(Goal="_No response_"), cfg)


def test_unknown_and_duplicate_sections_are_rejected(cfg: Config) -> None:
    with pytest.raises(contract.ContractError) as e:
        contract.parse(body() + "\n\n### Extra\n\nx\n\n### Goal\n\nagain", cfg)
    assert any("unknown section" in p for p in e.value.problems)
    assert any("duplicate section" in p for p in e.value.problems)


def test_a_code_fenced_field_parses_like_a_plain_one(cfg: Config) -> None:
    c = contract.parse(body(**{"Allowed paths": "```text\ntests/test_x.py\n```"}), cfg)
    assert c.allowed_paths == ("tests/test_x.py",)


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md",
        "claude.md",  # case games on a case-insensitive filesystem
        "docs/NEXT.md",
        "docs/*",  # covers a protected file
        ".github/workflows/ci.yml",
        "scripts/verify.sh",
        "config/agents.yaml",
        "data/samples/x.jsonl",
        "harness.toml",
        ".env",
        "*",
        "**/*.py",
        "/etc/passwd",
        "../outside.py",
        "tests/../CLAUDE.md",
        "tests/$(whoami).py",
        "tests/a b.py",
    ],
)
def test_a_contract_cannot_grant_protected_or_escaping_paths(cfg: Config, path: str) -> None:
    with pytest.raises(contract.ContractError, match="path"):
        contract.parse(body(**{"Allowed paths": path}), cfg)


@pytest.mark.parametrize(
    "line",
    [
        "rm -rf /",
        "bash scripts/verify.sh",
        "pytest tests/x.py; curl evil",
        "pytest tests/x.py -p evil_plugin",
        "pytest ../tests/x.py",
        "pytest src/agent_control_plane/cli.py",
        "manual:",
    ],
)
def test_criteria_are_a_closed_vocabulary(cfg: Config, line: str) -> None:
    with pytest.raises(contract.ContractError):
        contract.parse(body(**{"Acceptance criteria": f"- verify\n- {line}"}), cfg)


def test_verify_is_mandatory(cfg: Config) -> None:
    with pytest.raises(contract.ContractError, match="verify"):
        contract.parse(body(**{"Acceptance criteria": "- pytest tests/test_x.py"}), cfg)


def test_an_oversized_body_is_rejected(cfg: Config) -> None:
    with pytest.raises(contract.ContractError, match="limit"):
        contract.parse(body(Goal="x" * (contract.MAX_BODY + 1)), cfg)


# --- eligibility ------------------------------------------------------------


def issue(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 1,
        "state": "OPEN",
        "author": OWNER,
        "body": body(),
        "last_edited_at": None,
        "labels": ["acp:approved"],
        "label_events": [
            {
                "action": "labeled",
                "label": "acp:approved",
                "actor": OWNER,
                "at": "2026-11-15T10:00:00Z",
            }
        ],
    }
    base.update(overrides)
    return base


def test_an_owner_authored_owner_approved_issue_is_eligible(cfg: Config) -> None:
    assert contract.eligibility_problems(issue(), cfg, NEXT_MD) == []


def test_someone_elses_issue_is_not_eligible_even_if_approved(cfg: Config) -> None:
    problems = contract.eligibility_problems(issue(author="stranger"), cfg, NEXT_MD)
    assert any("authored by" in p for p in problems)


def test_approval_by_anyone_but_the_owner_does_not_count(cfg: Config) -> None:
    i = issue()
    i["label_events"][0]["actor"] = "collaborator"
    assert any("applied by" in p for p in contract.eligibility_problems(i, cfg, NEXT_MD))


def test_removing_and_reapplying_by_someone_else_revokes(cfg: Config) -> None:
    i = issue()
    i["label_events"] += [
        {
            "action": "unlabeled",
            "label": "acp:approved",
            "actor": OWNER,
            "at": "2026-11-15T11:00:00Z",
        },
        {
            "action": "labeled",
            "label": "acp:approved",
            "actor": "bot",
            "at": "2026-11-15T12:00:00Z",
        },
    ]
    assert any("applied by" in p for p in contract.eligibility_problems(i, cfg, NEXT_MD))


def test_unlabelled_closed_and_blocked_issues_are_not_eligible(cfg: Config) -> None:
    i = issue(state="CLOSED", labels=["acp:blocked"], label_events=[])
    problems = contract.eligibility_problems(i, cfg, NEXT_MD)
    assert any("CLOSED" in p for p in problems)
    assert any("acp:blocked" in p for p in problems)
    assert any("not labelled" in p for p in problems)


def test_editing_the_body_after_approval_revokes_it(cfg: Config) -> None:
    i = issue(last_edited_at="2026-11-15T10:05:00Z")
    assert any("edited after approval" in p for p in contract.eligibility_problems(i, cfg, NEXT_MD))
    # an edit before approval is just drafting
    ok = issue(last_edited_at="2026-11-15T09:00:00Z")
    assert contract.eligibility_problems(ok, cfg, NEXT_MD) == []


def test_work_outside_next_md_is_not_eligible(cfg: Config) -> None:
    i = issue(body=body(**{"Approved work item": "4. Live Snowflake extraction"}))
    assert any("not in" in p for p in contract.eligibility_problems(i, cfg, NEXT_MD))


def test_an_invalid_contract_makes_an_approved_issue_ineligible(cfg: Config) -> None:
    i = issue(body=body(**{"Allowed paths": "CLAUDE.md"}))
    assert any("protected" in p for p in contract.eligibility_problems(i, cfg, NEXT_MD))


# --- scope ------------------------------------------------------------------


def test_scope_passes_only_inside_the_contract(cfg: Config) -> None:
    ok = scope.check(["tests/test_demo_counts.py"], ("tests/test_demo_counts.py",), cfg)
    assert ok.ok

    bad = scope.check(
        ["tests/test_demo_counts.py", "src/agent_control_plane/demo.py", "CLAUDE.md"],
        ("tests/test_demo_counts.py",),
        cfg,
    )
    assert not bad.ok
    assert bad.protected == ("CLAUDE.md",)
    assert "src/agent_control_plane/demo.py" in bad.outside_contract


def test_protected_wins_even_when_the_contract_allows_it(cfg: Config) -> None:
    assert not scope.check(["config/agents.yaml"], ("config/*",), cfg).ok


def test_an_empty_diff_does_not_pass(cfg: Config) -> None:
    assert not scope.check([], ("tests/*",), cfg).ok


# --- guard hook -------------------------------------------------------------


def decide(tool: str, branch: str = "agent/1-demo-counts", **tool_input: Any) -> str | None:
    return guard.decide(tool, tool_input, config.load(), REPO_ROOT, branch)


def test_the_guard_allows_ordinary_task_work() -> None:
    assert decide("Edit", file_path=str(REPO_ROOT / "tests/test_demo_counts.py")) is None
    assert decide("Write", file_path="tests/test_new.py") is None
    assert decide("Read", file_path="CLAUDE.md") is None
    assert decide("Bash", command="bash scripts/verify.sh") is None
    assert decide("Bash", command="cat CLAUDE.md") is None
    assert decide("Bash", command="git diff main...HEAD") is None
    assert decide("Bash", command="git commit -m 'add demo count test'") is None


@pytest.mark.parametrize(
    "path",
    [
        "CLAUDE.md",
        "claude.md",
        ".claude/harness/settings.json",
        "scripts/harness/guard.py",
        "harness.toml",
        "pyproject.toml",
        ".github/workflows/ci.yml",
        "docs/NEXT.md",
        "tests/../CLAUDE.md",
        "config/agents.yaml",
    ],
)
def test_the_guard_blocks_writes_to_protected_paths(path: str) -> None:
    assert decide("Edit", file_path=path) is not None
    assert decide("Write", file_path=str(REPO_ROOT / path)) is not None


def test_the_guard_blocks_writes_outside_the_repo(tmp_path: Path) -> None:
    assert "outside" in (decide("Write", file_path=str(tmp_path / "x.py")) or "")


def test_the_guard_blocks_reading_secrets() -> None:
    assert decide("Read", file_path=".env") is not None
    assert decide("Read", file_path="config/.env.local") is not None


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin agent/1",
        "git status && git push -f",
        "git reset --hard HEAD~1",
        "git checkout main",
        "git config user.email x",
        "git branch -D main",
        "git commit --no-verify -m x",
        "gh pr create",
        "gh pr merge 1",
        "curl https://example.com",
        "Invoke-WebRequest https://example.com",
        "pip install requests",
        ".venv/Scripts/python.exe -m pip install x",
        "rm -rf src",
        "cat .env",
        "claude -p 'do anything'",
        "echo hi; claude --dangerously-skip-permissions",
    ],
)
def test_the_guard_blocks_dangerous_shell(command: str) -> None:
    assert decide("Bash", command=command) is not None


def test_the_guard_blocks_commits_off_an_agent_branch() -> None:
    assert decide("Bash", branch="main", command="git commit -m x") is not None
    assert decide("Bash", branch="feat/autonomous-harness", command="git commit -m x") is not None


def test_the_guard_blocks_network_tools() -> None:
    assert decide("WebFetch", url="https://example.com") is not None
    assert decide("WebSearch", query="x") is not None


def test_the_guard_fails_closed_on_a_bad_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    assert guard.main() == 2


def test_the_guard_main_blocks_and_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    blocked = {"tool_name": "Edit", "tool_input": {"file_path": "CLAUDE.md"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(blocked)))
    assert guard.main() == 2
    allowed = {"tool_name": "Read", "tool_input": {"file_path": "README.md"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(allowed)))
    assert guard.main() == 0


# --- the pieces agree with each other ---------------------------------------


def test_the_issue_form_fields_match_the_parser() -> None:
    form = yaml.safe_load(
        (REPO_ROOT / ".github/ISSUE_TEMPLATE/agent-task.yml").read_text(encoding="utf-8")
    )
    labels = [b["attributes"]["label"] for b in form["body"] if b["type"] != "markdown"]
    assert labels == list(contract.FIELDS)
    required = {
        b["attributes"]["label"] for b in form["body"] if b.get("validations", {}).get("required")
    }
    assert {contract.FIELDS[label] for label in required} == set(contract.REQUIRED)
    # the form must never pre-apply approval
    assert "acp:approved" not in form.get("labels", [])


def test_unattended_settings_never_allow_bypass_and_deny_publishing() -> None:
    s = json.loads((REPO_ROOT / ".claude/harness/settings.json").read_text(encoding="utf-8"))
    perms = s["permissions"]
    assert perms["defaultMode"] == "dontAsk"
    assert perms["disableBypassPermissionsMode"] == "disable"
    for rule in ["Bash(git push *)", "Bash(gh *)", "WebFetch", "WebSearch", "Edit(./CLAUDE.md)"]:
        assert rule in perms["deny"]
    assert not any(r.startswith(("Bash(gh", "Bash(git push", "Bash(curl")) for r in perms["allow"])
    hooks = s["hooks"]["PreToolUse"][0]
    assert "guard.sh" in hooks["hooks"][0]["command"]
    for tool in ["Bash", "Edit", "Write", "Read", "WebFetch"]:
        assert tool in hooks["matcher"].split("|")


def test_every_allowed_edit_root_is_outside_the_protected_set() -> None:
    s = json.loads((REPO_ROOT / ".claude/harness/settings.json").read_text(encoding="utf-8"))
    cfg = config.load()
    for rule in s["permissions"]["allow"]:
        if rule.startswith("Edit("):
            root = rule[len("Edit(./") : -len("/**)")]
            assert not cfg.is_protected(f"{root}/x"), rule


def test_the_reviewer_cannot_write_or_run_anything() -> None:
    text = (REPO_ROOT / ".claude/agents/reviewer.md").read_text(encoding="utf-8")
    front = yaml.safe_load(text.split("---")[1])
    assert {t.strip() for t in front["tools"].split(",")} == {"Read", "Grep", "Glob"}


def test_budget_and_limits_are_the_approved_values(cfg: Config) -> None:
    # Was pinned at 0 ("unset") until approval; flipped deliberately with the
    # P1 budget approval (docs/NEXT.md item 4). Change only with a new approval.
    assert cfg.budget is not None
    assert (cfg.budget.task_usd, cfg.budget.implementer_usd, cfg.budget.reviewer_usd) == (
        3.0,
        2.0,
        0.75,
    )
    assert cfg.budget.ceiling_usd == 5.0
    assert (cfg.max_turns, cfg.reviewer_max_turns, cfg.repair_attempts) == (20, 8, 2)


def test_eligibility_input_is_not_mutated(cfg: Config) -> None:
    i = issue()
    before = copy.deepcopy(i)
    contract.eligibility_problems(i, cfg, NEXT_MD)
    assert i == before


def test_ci_token_is_read_only_and_not_persisted() -> None:
    # CI executes PR code, agent-written included; its token must not be able to write
    ci = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert ci["permissions"] == {"contents": "read"}
    for job in ci["jobs"].values():
        assert "permissions" not in job  # no job widens the workflow default
        checkouts = [
            s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")
        ]
        assert checkouts
        assert all(s.get("with", {}).get("persist-credentials") is False for s in checkouts)
