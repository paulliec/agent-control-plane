"""The trusted verifier: judges one agent branch against a pinned baseline.

    python -m scripts.harness.gate --base <sha> --ref agent/1-demo-counts \
        [--bundle agent.bundle] --contract docs/harness/pilot-demo-counts.md \
        [--run-criteria]

Trust comes from where this runs, never from the candidate:

- This checkout *is* the baseline. Its HEAD must equal `--base` (a full SHA, not
  a moving ref) and its tree must be clean, so the gate code, harness.toml and
  the contract are exactly what was approved at that commit (G1). The contract
  is read with `git show <base>:<path>`, not from any working file.
- The candidate is only ever a ref. Without `--run-criteria` nothing from it is
  checked out or executed: ref name, ancestry (G5) and scope are git plumbing,
  so that half is safe on the host.
- `--run-criteria` executes the candidate's code (verify.sh, its tests). It
  refuses on a Windows host and unless ACP_SANDBOX=1, which only the disposable
  CI/sandbox environment sets. The flag prevents accidents; the isolation
  itself comes from that environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from scripts.harness import config, contract, scope

FULL_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
CANDIDATE_REF = "refs/harness/candidate"


class GateError(RuntimeError):
    pass


def sandboxed() -> bool:
    return os.name != "nt" and os.environ.get("ACP_SANDBOX") == "1"


def run_criterion(c: contract.Criterion, cfg: config.Config, cwd: Path) -> dict[str, Any]:
    if c.kind == "manual":
        return {"criterion": f"manual: {c.arg}", "status": "pending"}
    if c.kind == "verify":
        cmd = list(cfg.verify_command)
        label = "verify"
    else:
        # exit 5 means nothing collected, which fails here: a test that does not
        # exist has not passed
        cmd = [sys.executable, "-m", "pytest", c.arg, "-q", "--no-header"]
        label = f"pytest {c.arg}"
    # the candidate's src, not whatever copy of the package the image installed
    env = {**os.environ, "PYTHONPATH": str(cwd / "src")}
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)
    return {
        "criterion": label,
        "status": "pass" if proc.returncode == 0 else "fail",
        "exit_code": proc.returncode,
        "tail": (proc.stdout + proc.stderr).splitlines()[-15:],
    }


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


def evaluate(
    root: Path,
    base: str,
    ref: str,
    contract_path: str,
    *,
    bundle: Path | None = None,
    run_criteria: bool = False,
) -> dict[str, Any]:
    _check_baseline(root, base)
    cfg = config.load(root / "harness.toml")

    if not ref.startswith(cfg.branch_prefix):
        raise GateError(f"candidate ref must be {cfg.branch_prefix}*, not {ref!r}")
    if bundle is not None:
        _import_bundle(root, bundle, ref)
        tip_ref = CANDIDATE_REF
    else:
        tip_ref = f"refs/heads/{ref}"
    tip = scope.git(root, "rev-parse", "--verify", f"{tip_ref}^{{commit}}")[0]

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
        "criteria_run": run_criteria,
        "criteria": [],
    }
    if not scope.descends_from(root, base, tip):
        report.update(ok=False, error="candidate does not descend from the pinned base")
        return report

    sc = scope.check(scope.changed_files(root, base, tip), task.allowed_paths, cfg)
    report["scope"] = {
        "ok": sc.ok,
        "changed": sc.changed,
        "protected": sc.protected,
        "outside_contract": sc.outside_contract,
    }

    # no point running the suite over a diff that is already disqualified
    if sc.ok and run_criteria:
        if not sandboxed():
            raise GateError("refusing to execute candidate code outside a sandbox (ACP_SANDBOX=1)")
        with tempfile.TemporaryDirectory(prefix="acp-candidate-") as tmp:
            work = Path(tmp) / "work"
            scope.git(root, "worktree", "add", "--detach", "-q", str(work), tip)
            try:
                report["criteria"] = [run_criterion(c, cfg, work) for c in task.criteria]
            finally:
                scope.git(root, "worktree", "remove", "--force", str(work))
    elif sc.ok:
        report["criteria"] = [
            {
                "criterion": c.kind if c.kind == "verify" else f"{c.kind} {c.arg}",
                "status": "not_run",
            }
            for c in task.criteria
        ]

    # ok covers what was asked for; accepting a task needs ok with criteria_run
    report["ok"] = sc.ok and all(r["status"] != "fail" for r in report["criteria"])
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gate")
    ap.add_argument("--base", required=True, help="pinned baseline, full SHA")
    ap.add_argument("--ref", required=True, help="candidate branch name, e.g. agent/1-demo-counts")
    ap.add_argument("--contract", required=True, help="contract path inside the base commit")
    ap.add_argument("--bundle", type=Path, help="import the candidate from this git bundle")
    ap.add_argument("--run-criteria", action="store_true")
    args = ap.parse_args(argv)
    try:
        report = evaluate(
            config.REPO_ROOT,
            args.base,
            args.ref,
            args.contract,
            bundle=args.bundle,
            run_criteria=args.run_criteria,
        )
    except (GateError, contract.ContractError, subprocess.CalledProcessError) as e:
        report = {"ok": False, "error": str(e)}
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
