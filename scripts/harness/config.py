"""harness.toml, loaded once into a frozen dataclass.

Stdlib only: the guard hook imports this and must run on whatever Python the
host has, before any venv exists.

`[budget]` and `[sandbox]` parse as optional so the guard keeps working without
them; the ledger and the gate refuse to run when they are missing or invalid.
"""

from __future__ import annotations

import fnmatch
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Budget:
    task_usd: float
    implementer_usd: float
    reviewer_usd: float
    ceiling_usd: float


@dataclass(frozen=True)
class Sandbox:
    image: str
    install: tuple[str, ...]
    timeout_s: int
    cpus: str
    memory: str
    pids: int


@dataclass(frozen=True)
class Config:
    owner: str
    integration_branch: str
    branch_prefix: str
    approved_work: str
    approved_label: str
    blocked_label: str
    max_turns: int
    reviewer_max_turns: int
    repair_attempts: int
    verify_command: tuple[str, ...]
    protected: tuple[str, ...]
    budget: Budget | None = None
    sandbox: Sandbox | None = None

    def is_protected(self, rel_path: str) -> bool:
        return matches_any(rel_path, self.protected)


def matches_any(rel_path: str, globs: tuple[str, ...]) -> bool:
    # lowercased both sides: NTFS is case-insensitive, so `claude.md` is
    # CLAUDE.md and must not slip past a case-sensitive match
    p = rel_path.replace("\\", "/").lower()
    return any(fnmatch.fnmatchcase(p, g.lower()) for g in globs)


def _budget(raw: dict[str, Any] | None) -> Budget | None:
    if raw is None:
        return None
    try:
        return Budget(
            task_usd=float(raw["task_usd"]),
            implementer_usd=float(raw["implementer_usd"]),
            reviewer_usd=float(raw["reviewer_usd"]),
            ceiling_usd=float(raw["ceiling_usd"]),
        )
    except (KeyError, TypeError, ValueError):
        return None  # the ledger reports "missing or invalid" and refuses


def _sandbox(raw: dict[str, Any] | None) -> Sandbox | None:
    if raw is None:
        return None
    try:
        return Sandbox(
            image=str(raw["image"]),
            install=tuple(str(x) for x in raw["install"]),
            timeout_s=int(raw["timeout_s"]),
            cpus=str(raw["cpus"]),
            memory=str(raw["memory"]),
            pids=int(raw["pids"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def load(path: Path | None = None) -> Config:
    raw = tomllib.loads((path or REPO_ROOT / "harness.toml").read_text(encoding="utf-8"))
    repo, labels, limits = raw["repo"], raw["labels"], raw["limits"]
    return Config(
        owner=repo["owner"],
        integration_branch=repo["integration_branch"],
        branch_prefix=repo["branch_prefix"],
        approved_work=repo["approved_work"],
        approved_label=labels["approved"],
        blocked_label=labels["blocked"],
        max_turns=int(limits["max_turns"]),
        reviewer_max_turns=int(limits["reviewer_max_turns"]),
        repair_attempts=int(limits["repair_attempts"]),
        verify_command=tuple(raw["verify"]["command"]),
        protected=tuple(raw["protected"]["paths"]),
        budget=_budget(raw.get("budget")),
        sandbox=_sandbox(raw.get("sandbox")),
    )
