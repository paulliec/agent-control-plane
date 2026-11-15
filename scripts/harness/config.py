"""harness.toml, loaded once into a frozen dataclass.

Stdlib only: the guard hook imports this and must run on whatever Python the
host has, before any venv exists.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    owner: str
    base_branch: str
    branch_prefix: str
    approved_work: str
    approved_label: str
    blocked_label: str
    max_turns: int
    repair_attempts: int
    max_budget_usd: float
    verify_command: tuple[str, ...]
    protected: tuple[str, ...]

    def is_protected(self, rel_path: str) -> bool:
        return matches_any(rel_path, self.protected)


def matches_any(rel_path: str, globs: tuple[str, ...]) -> bool:
    # lowercased both sides: NTFS is case-insensitive, so `claude.md` is
    # CLAUDE.md and must not slip past a case-sensitive match
    p = rel_path.replace("\\", "/").lower()
    return any(fnmatch.fnmatchcase(p, g.lower()) for g in globs)


def load(path: Path | None = None) -> Config:
    raw = tomllib.loads((path or REPO_ROOT / "harness.toml").read_text(encoding="utf-8"))
    repo, labels, limits = raw["repo"], raw["labels"], raw["limits"]
    return Config(
        owner=repo["owner"],
        base_branch=repo["base_branch"],
        branch_prefix=repo["branch_prefix"],
        approved_work=repo["approved_work"],
        approved_label=labels["approved"],
        blocked_label=labels["blocked"],
        max_turns=int(limits["max_turns"]),
        repair_attempts=int(limits["repair_attempts"]),
        max_budget_usd=float(limits["max_budget_usd"]),
        verify_command=tuple(raw["verify"]["command"]),
        protected=tuple(raw["protected"]["paths"]),
    )
