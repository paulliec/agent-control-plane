"""Task contracts: a GitHub issue-form body, parsed and checked for eligibility.

The body is untrusted even after approval. Nothing in it is executed or put on
a command line, and nothing in it can widen what the harness may touch: paths
are validated against harness.toml, and acceptance criteria are a closed
vocabulary the gate maps onto fixed commands.

    python -m scripts.harness.contract check-file docs/harness/pilot-demo-counts.md
    python -m scripts.harness.contract check-issue 12      # read-only gh query
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from scripts.harness import config
from scripts.harness.config import Config

# issue-form label -> field. Must match .github/ISSUE_TEMPLATE/agent-task.yml.
FIELDS = {
    "Goal": "goal",
    "Approved work item": "approved_item",
    "Allowed paths": "allowed_paths",
    "Acceptance criteria": "criteria",
    "Stop conditions": "stop_conditions",
    "Out of scope": "out_of_scope",
    # Set when the issue is approved for execution: the integration commit the
    # run must start from. Not part of the contract digest, so the committed
    # contract file (which cannot contain its own commit SHA) still matches.
    "Pinned base": "pinned_base",
}
REQUIRED = ("goal", "approved_item", "allowed_paths", "criteria", "stop_conditions")
NO_RESPONSE = "_No response_"  # what GitHub renders for an empty form field
MAX_BODY = 20_000

PATH_CHARS = re.compile(r"^[A-Za-z0-9_.*/-]+$")
PYTEST_NODE = re.compile(r"^tests/[A-Za-z0-9_/]+\.py(::[A-Za-z0-9_]+)*$")
APPROVED_ITEM = re.compile(r"^(\d+)\.\s+\*\*(.+?)\*\*", re.MULTILINE)
FULL_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")

Kind = Literal["verify", "pytest", "manual"]


class ContractError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Criterion:
    kind: Kind
    arg: str = ""

    @property
    def automated(self) -> bool:
        return self.kind != "manual"


@dataclass(frozen=True)
class Contract:
    goal: str
    approved_item: str
    allowed_paths: tuple[str, ...]
    criteria: tuple[Criterion, ...]
    stop_conditions: str
    out_of_scope: str
    pinned_base: str = ""


def _lines(text: str) -> list[str]:
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            continue
        for prefix in ("- [ ] ", "- [x] ", "- ", "* "):
            if line.startswith(prefix):
                line = line[len(prefix) :].strip()
                break
        if line:
            out.append(line.strip("`"))
    return out


def _sections(body: str) -> tuple[dict[str, str], list[str]]:
    sections: dict[str, list[str]] = {}
    problems: list[str] = []
    current: str | None = None
    for line in body.splitlines():
        if line.startswith("### "):
            heading = line[4:].strip()
            if heading not in FIELDS:
                problems.append(f"unknown section {heading!r}")
                current = None
            elif FIELDS[heading] in sections:
                problems.append(f"duplicate section {heading!r}")
                current = None
            else:
                current = FIELDS[heading]
                sections[current] = []
        elif current is not None:
            sections[current].append(line)
    joined = {k: "\n".join(v).strip() for k, v in sections.items()}
    return {k: ("" if v == NO_RESPONSE else v) for k, v in joined.items()}, problems


def parse_criterion(line: str) -> Criterion:
    if line == "verify":
        return Criterion("verify")
    if line.startswith("pytest "):
        node = line[len("pytest ") :].strip()
        if not PYTEST_NODE.fullmatch(node):
            raise ContractError([f"criterion {line!r}: not a plain tests/ node id"])
        return Criterion("pytest", node)
    if line.startswith("manual:"):
        text = line[len("manual:") :].strip()
        if not text:
            raise ContractError(["criterion 'manual:' has no text"])
        return Criterion("manual", text)
    raise ContractError(
        [f"criterion {line!r}: expected 'verify', 'pytest <node>' or 'manual: ...'"]
    )


def path_problems(pattern: str, cfg: Config) -> list[str]:
    if not PATH_CHARS.fullmatch(pattern):
        return [f"path {pattern!r}: only letters, digits, _ . - / * allowed"]
    if pattern.startswith(("/", "*")) or ".." in pattern.split("/"):
        return [f"path {pattern!r}: must be a relative path inside the repo, not a wildcard root"]
    # Reject a grant that overlaps protected paths either way round. The scope
    # check would refuse those files anyway; this makes the contract say so.
    probes = [g.replace("*", "x") for g in cfg.protected]
    if cfg.is_protected(pattern) or any(config.matches_any(p, (pattern,)) for p in probes):
        return [f"path {pattern!r}: overlaps a protected path"]
    return []


def parse(body: str, cfg: Config) -> Contract:
    if len(body) > MAX_BODY:
        raise ContractError([f"body is {len(body)} chars, over the {MAX_BODY} limit"])
    fields, problems = _sections(body)
    problems += [f"missing section {k!r}" for k in REQUIRED if not fields.get(k)]

    paths = tuple(_lines(fields.get("allowed_paths", "")))
    for p in paths:
        problems += path_problems(p, cfg)

    criteria: list[Criterion] = []
    for line in _lines(fields.get("criteria", "")):
        try:
            criteria.append(parse_criterion(line))
        except ContractError as e:
            problems += e.problems
    # The deterministic gate is not optional, whatever the contract says. No
    # precondition: a field that normalises to nothing (an empty code fence)
    # must fail here, not slip through as "no criteria to check".
    if not any(c.kind == "verify" for c in criteria):
        problems.append("criteria must include 'verify'")
    pinned = fields.get("pinned_base", "").strip()
    if pinned and not FULL_SHA.fullmatch(pinned):
        problems.append(f"pinned base {pinned!r} is not a full lowercase commit SHA")

    if problems:
        raise ContractError(problems)
    return Contract(
        goal=fields["goal"],
        approved_item=fields["approved_item"].strip().rstrip("."),
        allowed_paths=paths,
        criteria=tuple(criteria),
        stop_conditions=fields["stop_conditions"],
        out_of_scope=fields.get("out_of_scope", ""),
        pinned_base=fields.get("pinned_base", "").strip(),
    )


def digest(c: Contract) -> str:
    """Canonical sha256 of what was approved, excluding the pinned base.

    Hashes the parsed fields, whitespace-normalised, rather than raw text:
    an issue and the committed file then match when they say the same
    thing, however GitHub re-wraps lines or line endings.
    """
    fields = {k: v for k, v in asdict(c).items() if k != "pinned_base"}
    canon = json.dumps(_normalise(fields), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _normalise(v: Any) -> Any:
    if isinstance(v, str):
        return " ".join(v.split())
    if isinstance(v, dict):
        return {k: _normalise(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_normalise(x) for x in v]
    return v


def approved_items(next_md: str) -> list[str]:
    """`N. Title` for each item under NEXT.md's 'Current approved work'."""
    start = next_md.find("## Current approved work")
    if start < 0:
        return []
    end = next_md.find("\n## ", start + 1)
    section = next_md[start : end if end > 0 else None]
    return [f"{n}. {t.strip().rstrip('.')}" for n, t in APPROVED_ITEM.findall(section)]


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def eligibility_problems(issue: dict[str, Any], cfg: Config, next_md: str) -> list[str]:
    """Empty list means the issue may be worked. `issue` is the shape fetch_issue returns."""
    problems: list[str] = []
    if issue["state"] != "OPEN":
        problems.append(f"issue is {issue['state']}")
    if issue["author"] != cfg.owner:
        problems.append(f"authored by {issue['author']!r}, not {cfg.owner!r}")
    if cfg.blocked_label in issue["labels"]:
        problems.append(f"labelled {cfg.blocked_label}")

    events = [e for e in issue["label_events"] if e["label"] == cfg.approved_label]
    last = max(events, key=lambda e: _at(e["at"])) if events else None
    if cfg.approved_label not in issue["labels"] or last is None or last["action"] != "labeled":
        problems.append(f"not labelled {cfg.approved_label}")
    elif last["actor"] != cfg.owner:
        problems.append(f"{cfg.approved_label} applied by {last['actor']!r}, not {cfg.owner!r}")
    elif issue["last_edited_at"] and _at(issue["last_edited_at"]) > _at(last["at"]):
        # approval covers the text that was approved, not whatever it says now
        problems.append("body edited after approval; re-apply the label to approve this text")

    try:
        contract = parse(issue["body"], cfg)
    except ContractError as e:
        return problems + e.problems
    items = approved_items(next_md)
    if contract.approved_item not in items:
        problems.append(f"approved work item {contract.approved_item!r} is not in {items}")
    return problems


_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      number state body lastEditedAt
      author { login }
      labels(first: 50) { nodes { name } }
      timelineItems(last: 100, itemTypes: [LABELED_EVENT, UNLABELED_EVENT]) {
        nodes {
          __typename
          ... on LabeledEvent { createdAt actor { login } label { name } }
          ... on UnlabeledEvent { createdAt actor { login } label { name } }
        }
      }
    }
  }
}
"""


def fetch_issue(number: int) -> dict[str, Any]:
    """Read-only. Uses whatever `gh` is authenticated as."""
    repo = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    owner, name = repo.split("/")
    fields = [f"query={_QUERY}", f"owner={owner}", f"name={name}"]
    out = subprocess.run(
        ["gh", "api", "graphql", *(a for f in fields for a in ("-f", f)), "-F", f"number={number}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    raw = json.loads(out)["data"]["repository"]["issue"]
    return {
        "number": raw["number"],
        "state": raw["state"],
        "author": (raw["author"] or {}).get("login"),
        "body": raw["body"],
        "last_edited_at": raw["lastEditedAt"],
        "labels": [n["name"] for n in raw["labels"]["nodes"]],
        "label_events": [
            {
                "action": "labeled" if n["__typename"] == "LabeledEvent" else "unlabeled",
                "label": n["label"]["name"],
                "actor": (n["actor"] or {}).get("login"),
                "at": n["createdAt"],
            }
            for n in raw["timelineItems"]["nodes"]
        ],
    }


def match_problems(task: Contract, expected: Path, cfg: Config) -> list[str]:
    """The approved issue must say exactly what the committed contract says."""
    try:
        committed = parse(expected.read_text(encoding="utf-8"), cfg)
    except (OSError, ContractError) as e:
        return [f"committed contract {str(expected)!r} is unusable: {e}"]
    if digest(task) != digest(committed):
        return [f"issue contract does not match {expected.as_posix()} (digest differs)"]
    return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="contract")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-file").add_argument("path", type=Path)
    issue_cmd = sub.add_parser("check-issue")
    issue_cmd.add_argument("number", type=int)
    issue_cmd.add_argument(
        "--expect-file",
        type=Path,
        help="committed contract the issue must match (by digest, pinned base excluded)",
    )
    args = ap.parse_args(argv)

    cfg = config.load()
    next_md = (config.REPO_ROOT / cfg.approved_work).read_text(encoding="utf-8")

    if args.cmd == "check-issue":
        issue = fetch_issue(args.number)
        problems = eligibility_problems(issue, cfg, next_md)
        body = issue["body"]
        if args.expect_file and not problems:
            problems = match_problems(parse(body, cfg), args.expect_file, cfg)
    else:
        body = args.path.read_text(encoding="utf-8")
        problems = []
        try:
            item = parse(body, cfg).approved_item
            if item not in approved_items(next_md):
                problems.append(f"approved work item {item!r} is not in {cfg.approved_work}")
        except ContractError as e:
            problems = e.problems

    result: dict[str, Any] = {"eligible": not problems, "problems": problems}
    if not problems:
        task = parse(body, cfg)
        result["contract"] = asdict(task)
        result["contract_sha256"] = digest(task)
    json.dump(result, sys.stdout, indent=2)
    print()
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
