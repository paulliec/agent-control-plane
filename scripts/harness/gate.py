"""The trusted verifier: judges one agent branch against a pinned baseline.

    python -m scripts.harness.gate --base <sha> --ref agent/7 \
        [--bundle agent.bundle --expect-bundle-sha256 <hex> --expect-tip <sha>] \
        --contract docs/harness/pilot-demo-counts.md \
        [--run-criteria --wheels <dir>]

Trust comes from where this runs, never from the candidate:

- This checkout *is* the baseline. Its HEAD must equal `--base` (a full SHA, not
  a moving ref) and its tree must be clean, so the gate code, harness.toml and
  the contract are exactly what was approved at that commit (G1). The contract
  is read with `git show <base>:<path>`, not from any working file.
- The candidate is only ever a ref, imported from a bundle whose digest and tip
  can be pinned by the caller (artifact substitution). Without `--run-criteria`
  nothing from it is checked out or executed: ref name, ancestry (G5), scope on
  every commit, and a secret scan of every added line are git plumbing, so that
  half is safe on the host.
- `--run-criteria` executes candidate code, and only through the container in
  `sandbox.py`. It refuses on a Windows host and unless ACP_SANDBOX=1, which
  only the disposable CI job sets; that flag prevents accidents, the container
  is the isolation. Two runs, each in its own fresh tree and container:
    trusted_baseline    the candidate's code under the base commit's tests
    candidate_authored  the contract's criteria, the candidate's own tests included
  Only the first can't be influenced by candidate-written tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from scripts.harness import config, contract, sandbox, scope

FULL_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
CANDIDATE_REF = "refs/harness/candidate"

# High-signal token shapes only; a hit names the file and the pattern, never the value.
SECRET_PATTERNS = {
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "github-token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "github-pat": re.compile(r"github_pat_[A-Za-z0-9_]{30,}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private-key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "slack-token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "harness-canary": re.compile(r"ACP_CANARY_[A-Za-z0-9]{8,}"),
}

Executor = Callable[[list[str], Path], tuple[int, str]]


class GateError(RuntimeError):
    pass


def sandboxed() -> bool:
    return os.name != "nt" and os.environ.get("ACP_SANDBOX") == "1"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_criterion(
    c: contract.Criterion, cfg: config.Config, cwd: Path, execute: Executor | None = None
) -> dict[str, Any]:
    if c.kind == "manual":
        return {"criterion": f"manual: {c.arg}", "status": "pending"}
    if c.kind == "verify":
        cmd = list(cfg.verify_command)
        label = "verify"
        report = None
    else:
        # A zero exit alone proves nothing: a module calling os._exit(0) at import
        # exits 0 with nothing run. So pytest must also leave a JUnit report under
        # a name chosen now, after the candidate was fixed, naming the same nonce.
        nonce = secrets.token_hex(12)
        report = cwd / f".acp-junit-{nonce}.xml"
        cmd = ["python", "-m", "pytest", c.arg, "-q", "--no-header", "-p", "no:cacheprovider",
               f"--junitxml={report.name}", "-o", "junit_family=xunit2",
               "-o", f"junit_suite_name={nonce}"]  # fmt: skip
        label = f"pytest {c.arg}"
    if execute is None:
        return {"criterion": label, "status": "fail", "exit_code": None, "tail": ["no sandbox"]}
    if report is not None and (report.exists() or report.is_symlink()):
        return {"criterion": label, "status": "fail", "exit_code": None,
                "tail": ["a report already exists at the fresh report path"]}  # fmt: skip
    code, output = execute(cmd, cwd)
    result: dict[str, Any] = {
        "criterion": label,
        "status": "pass" if code == 0 else "fail",
        "exit_code": code,
        "tail": output.splitlines()[-15:],
    }
    if report is not None:
        problems, counts = junit_problems(report, nonce, c.arg)
        report.unlink(missing_ok=True)
        result["report"] = {"problems": problems, **counts}
        if problems:
            result["status"] = "fail"
    return result


def _test_id(target: str) -> str:
    """`tests/test_x.py::Cls::test_a` -> `tests.test_x.Cls.test_a`, as JUnit names it."""
    path, _, rest = target.partition("::")
    module = path.removesuffix(".py").replace("/", ".")
    return ".".join([module, *[p for p in rest.split("::") if p]])


def junit_problems(report: Path, nonce: str, target: str) -> tuple[list[str], dict[str, int]]:
    """Why this report doesn't show a real, complete, passing run of `target`.

    Evidence of integrity, not proof: candidate code runs in the same process as
    pytest and could, in principle, write a convincing report itself."""
    counts = {"passed": 0, "matched": 0, "failed": 0, "skipped": 0}
    if report.is_symlink() or not report.is_file():
        return ["no test report: the run ended before pytest finished"], counts
    if report.stat().st_size > 5_000_000:
        return ["test report is implausibly large"], counts
    try:
        root = ElementTree.fromstring(report.read_bytes())
    except ElementTree.ParseError:
        return ["test report is malformed"], counts
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if root.tag not in ("testsuite", "testsuites") or not suites:
        return ["test report has no test suite"], counts
    if any(s.get("name") != nonce for s in suites):
        return ["test report is not from this execution"], counts

    problems: list[str] = []
    want = _test_id(target)
    cases = [case for s in suites for case in s.iter("testcase")]
    try:
        declared = sum(int(s.get("tests", "-1")) for s in suites)
        declared_bad = sum(int(s.get("failures", "0")) + int(s.get("errors", "0")) for s in suites)
    except ValueError:
        return ["test report has non-numeric counts"], counts
    if declared != len(cases):
        problems.append("test report is incomplete: declared and listed tests differ")
    for case in cases:
        tags = {child.tag for child in case}
        if tags & {"failure", "error"}:
            counts["failed"] += 1
        elif "skipped" in tags:
            counts["skipped"] += 1
        else:
            counts["passed"] += 1
            full = f"{case.get('classname', '')}.{case.get('name', '').split('[')[0]}"
            if full == want or full.startswith(want + "."):
                counts["matched"] += 1
    if counts["failed"] or declared_bad:
        problems.append("test report records failures or errors")
    if not cases:
        problems.append("test report lists no tests")
    elif not counts["matched"]:
        problems.append(f"no passing test belongs to {target}")
    return problems, counts


def secret_hits(root: Path, base: str, tip: str) -> list[dict[str, str]]:
    """Secret-shaped strings in any line added by any commit in base..tip."""
    log = subprocess.run(
        ["git", "log", "-p", "--text", "--no-renames", "--no-color", "--format=", f"{base}..{tip}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    hits: set[tuple[str, str]] = set()
    current = "?"
    for line in log.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+"):
            hits.update((current, n) for n, rx in SECRET_PATTERNS.items() if rx.search(line))
    return [{"file": f, "pattern": n} for f, n in sorted(hits)]


def _check_baseline(root: Path, base: str) -> None:
    if not FULL_SHA.fullmatch(base):
        raise GateError(f"--base must be a full commit SHA, not {base!r}")
    head = scope.git(root, "rev-parse", "HEAD")[0]
    if head != base:
        raise GateError(f"trusted checkout is at {head}, not the pinned base {base}")
    if scope.git(root, "status", "--porcelain", "--untracked-files=all"):
        raise GateError("trusted checkout has local changes; the gate must run from the clean base")


def _import_bundle(root: Path, bundle: Path, ref: str) -> None:
    heads = [line.split()[1] for line in scope.git(root, "bundle", "list-heads", str(bundle))]
    if heads != [f"refs/heads/{ref}"]:
        raise GateError(f"bundle must carry exactly refs/heads/{ref}, has {heads}")
    scope.git(root, "bundle", "verify", "-q", str(bundle))
    # quarantined outside refs/heads: never checked out, never pushed
    scope.git(root, "fetch", "--no-tags", "-q", str(bundle), f"+refs/heads/{ref}:{CANDIDATE_REF}")


def materialize(root: Path, ref: str, dest: Path) -> Path:
    """A self-contained clone of `ref` at `dest`: its own .git, so it can be
    bind-mounted into a container on its own, unlike a worktree. Fetching and
    checking out run no candidate code (fresh repo, no hooks, no filters)."""
    dest.mkdir(parents=True)
    scope.git(dest, "init", "-q")
    scope.git(dest, "fetch", "-q", "--no-tags", str(root), f"+{ref}:refs/heads/candidate")
    scope.git(dest, "checkout", "-q", "candidate")
    return dest


def baseline_tree(root: Path, ref: str, base: str, dest: Path) -> Path:
    """The candidate's code under the base commit's tests: every test file the
    candidate added is gone, every one it changed is back to the base version."""
    materialize(root, ref, dest)
    # restore is no-overlay by default: paths absent from `base` are deleted, so
    # this one command both drops added tests and reverts changed ones
    scope.git(dest, "restore", "--source", base, "--staged", "--worktree", "--", "tests")
    return dest


def evaluate(
    root: Path,
    base: str,
    ref: str,
    contract_path: str,
    *,
    bundle: Path | None = None,
    run_criteria: bool = False,
    wheels: Path | None = None,
    execute: Executor | None = None,
    expect_bundle_sha256: str | None = None,
    expect_tip: str | None = None,
) -> dict[str, Any]:
    _check_baseline(root, base)
    cfg = config.load(root / "harness.toml")

    if not ref.startswith(cfg.branch_prefix):
        raise GateError(f"candidate ref must be {cfg.branch_prefix}*, not {ref!r}")
    bundle_sha256 = None
    if bundle is not None:
        bundle_sha256 = sha256_file(bundle)
        if expect_bundle_sha256 is not None and bundle_sha256 != expect_bundle_sha256:
            raise GateError("bundle digest does not match the one recorded upstream")
        _import_bundle(root, bundle, ref)
        tip_ref = CANDIDATE_REF
    else:
        tip_ref = f"refs/heads/{ref}"
    tip = scope.git(root, "rev-parse", "--verify", f"{tip_ref}^{{commit}}")[0]
    if expect_tip is not None and tip != expect_tip:
        raise GateError(f"candidate tip {tip} is not the recorded tip {expect_tip}")

    # the approved text at the pinned commit, whatever the candidate did to it
    body = subprocess.run(
        ["git", "show", f"{base}:{contract_path}"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    task = contract.parse(body, cfg)

    report: dict[str, Any] = {
        "base": base,
        "ref": ref,
        "tip": tip,
        "bundle_sha256": bundle_sha256,
        "contract_sha256": contract.digest(task),
        "criteria_run": run_criteria,
        "criteria": [],
    }
    if not scope.descends_from(root, base, tip):
        report.update(ok=False, error="candidate does not descend from the pinned base")
        return report
    # agents can't merge (guard), and a merge's changes depend on which parent
    # you diff against, so a candidate with one is refused rather than judged
    if merges := scope.merge_commits(root, base, tip):
        report.update(ok=False, error=f"candidate contains merge commits: {merges}")
        return report

    sc = scope.check(scope.changed_files(root, base, tip), task.allowed_paths, cfg)
    report["scope"] = {
        "ok": sc.ok,
        "changed": sc.changed,
        "protected": sc.protected,
        "outside_contract": sc.outside_contract,
    }
    report["secrets"] = secret_hits(root, base, tip)
    clean = sc.ok and not report["secrets"]

    # no point running the suite over a diff that is already disqualified
    if clean and run_criteria:
        if not sandboxed():
            raise GateError("refusing to execute candidate code outside a sandbox (ACP_SANDBOX=1)")
        if execute is None:
            sb = cfg.sandbox
            if sb is None or wheels is None or not wheels.is_dir():
                report.update(ok=False, error="sandbox prerequisites missing ([sandbox], --wheels)")
                return report
            execute = lambda cmd, cwd: sandbox.run(sb, cwd, wheels, cmd)  # noqa: E731
        with tempfile.TemporaryDirectory(prefix="acp-gate-") as tmp:
            base_tree = baseline_tree(root, tip_ref, base, Path(tmp) / "baseline")
            verify = contract.Criterion("verify")
            report["baseline"] = {
                "label": "trusted_baseline",
                **run_criterion(verify, cfg, base_tree, execute),
            }
            report["criteria_label"] = "candidate_authored"
            # a fresh clone per executable criterion: one criterion's run (say,
            # verify.sh rewriting files) can't change the tree the next one judges
            report["criteria"] = [
                run_criterion(c, cfg, Path(tmp), execute)
                if c.kind == "manual"
                else run_criterion(
                    c, cfg, materialize(root, tip_ref, Path(tmp) / f"candidate-{i}"), execute
                )
                for i, c in enumerate(task.criteria)
            ]
    elif clean:
        report["criteria"] = [
            {
                "criterion": c.kind if c.kind == "verify" else f"{c.kind} {c.arg}",
                "status": "not_run",
            }
            for c in task.criteria
        ]

    # ok covers what was asked for; accepting a task needs ok with criteria_run.
    # A criteria run that never passed verify, on the baseline and the
    # candidate, is never ok, whatever the parser let through.
    verified = any(r["criterion"] == "verify" and r["status"] == "pass" for r in report["criteria"])
    baseline_ok = report.get("baseline", {}).get("status") == "pass"
    report["ok"] = (
        clean
        and all(r["status"] != "fail" for r in report["criteria"])
        and (not run_criteria or (verified and baseline_ok))
    )
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gate")
    ap.add_argument("--base", required=True, help="pinned baseline, full SHA")
    ap.add_argument("--ref", required=True, help="candidate branch name, e.g. agent/7")
    ap.add_argument("--contract", required=True, help="contract path inside the base commit")
    ap.add_argument("--bundle", type=Path, help="import the candidate from this git bundle")
    ap.add_argument("--expect-bundle-sha256", help="refuse a bundle with any other digest")
    ap.add_argument("--expect-tip", help="refuse a candidate with any other tip")
    ap.add_argument("--run-criteria", action="store_true")
    ap.add_argument("--wheels", type=Path, help="offline wheelhouse built at the base")
    args = ap.parse_args(argv)
    try:
        report = evaluate(
            config.REPO_ROOT,
            args.base,
            args.ref,
            args.contract,
            bundle=args.bundle,
            run_criteria=args.run_criteria,
            wheels=args.wheels,
            expect_bundle_sha256=args.expect_bundle_sha256,
            expect_tip=args.expect_tip,
        )
    except (GateError, contract.ContractError, subprocess.CalledProcessError) as e:
        report = {"ok": False, "error": str(e)}
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
