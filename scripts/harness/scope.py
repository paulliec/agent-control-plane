"""Which files did a candidate branch change, and was it allowed to?

Git plumbing only: nothing here checks out or executes the candidate, so it is
safe to run on the host against an untrusted branch. Only commits are judged —
the candidate arrives as a git bundle, so uncommitted or ignored files never
reach the verifier.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from scripts.harness.config import Config, matches_any


@dataclass(frozen=True)
class ScopeResult:
    changed: tuple[str, ...]
    protected: tuple[str, ...]
    outside_contract: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return bool(self.changed) and not self.protected and not self.outside_contract


def check(changed: list[str], allowed: tuple[str, ...], cfg: Config) -> ScopeResult:
    files = tuple(sorted(set(changed)))
    return ScopeResult(
        changed=files,
        protected=tuple(f for f in files if cfg.is_protected(f)),
        outside_contract=tuple(f for f in files if not matches_any(f, allowed)),
    )


def git(repo: Path, *args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    return [line for line in out.splitlines() if line]


def changed_files(repo: Path, base: str, tip: str) -> list[str]:
    # --no-renames: a rename would otherwise list only the destination, hiding
    # that a protected file was moved away. Git quotes unusual names
    # ("tests/caf\303\251.py"); a quoted name matches no contract glob, so it
    # fails as outside_contract — closed, not open.
    return git(repo, "diff", "--no-renames", "--name-only", base, tip)


def descends_from(repo: Path, base: str, tip: str) -> bool:
    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base, tip], cwd=repo, capture_output=True
    )
    return proc.returncode == 0


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD")[0]
