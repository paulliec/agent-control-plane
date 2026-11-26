"""Durable per-task accounting: reserve before spending, settle afterwards.

Records are issue comments written by the workflow token (author
`github-actions[bot]`), each a marker line plus one JSON object:

    reserve   posted by `authorize` BEFORE any model call, for one run attempt
    account   posted by `account` afterwards with the costs the engine reported

Spend is counted from reservations, not from accounts: a reservation with no
account (cancelled, crashed, runner lost) counts at its full allocation, and
so does any cost reported as missing. An account can only lower a
reservation's count to what was actually reported. A final always() job is
therefore a refinement, never the thing that makes accounting durable.

Everything fails closed: an invalid budget, a malformed record from the bot,
or comments that can't be read all refuse the run rather than guess.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from scripts.harness.config import Budget, Config

BOT = "github-actions[bot]"
MARKER = "<!-- acp-ledger:v1 -->"
_JSON = re.compile(r"```json\n(.*?)\n```", re.DOTALL)


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Record:
    kind: str
    at: datetime
    data: dict[str, Any]
    id: int = 0  # comment id: breaks ties between equal timestamps


def _at(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def budget_problems(b: Budget | None) -> list[str]:
    if b is None:
        return ["[budget] is missing or malformed in harness.toml"]
    problems = [
        f"budget {name} must be a positive number, not {v!r}"
        for name, v in (
            ("task_usd", b.task_usd),
            ("implementer_usd", b.implementer_usd),
            ("reviewer_usd", b.reviewer_usd),
            ("ceiling_usd", b.ceiling_usd),
        )
        if not v > 0  # also catches NaN
    ]
    if b.implementer_usd + b.reviewer_usd > b.task_usd:
        problems.append("implementer_usd + reviewer_usd exceeds task_usd")
    if b.task_usd > b.ceiling_usd:
        problems.append(f"task_usd {b.task_usd} exceeds the ceiling {b.ceiling_usd}")
    return problems


def parse(comments: list[dict[str, Any]]) -> list[Record]:
    """Ledger records from issue comments. Only the bot's count; a bot comment
    carrying the marker that doesn't parse means state is unknown: refuse."""
    out = []
    for c in comments:
        body = str(c.get("body", ""))
        if c.get("author") != BOT or not body.startswith(MARKER):
            continue
        m = _JSON.search(body)
        try:
            data = json.loads(m.group(1)) if m else None
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict) or data.get("type") not in ("reserve", "account"):
            raise LedgerError(f"unreadable ledger record in comment at {c.get('created_at')}")
        # the bot never edits a record; an edited one has been tampered with
        if c.get("updated_at", c["created_at"]) != c["created_at"]:
            raise LedgerError(f"ledger record at {c['created_at']} was edited after it was written")
        out.append(Record(data["type"], _at(str(c["created_at"])), data, int(c.get("id") or 0)))
    return out


def render(data: dict[str, Any]) -> str:
    return f"{MARKER}\n```json\n{json.dumps(data, sort_keys=True)}\n```"


def _key(d: dict[str, Any]) -> tuple[str, str]:
    return str(d.get("run_id")), str(d.get("run_attempt"))


def _cost(v: Any, allocation: float) -> float:
    # absent data stays absent elsewhere; here, for safety, it costs the full allocation
    if v == "not_run":
        return 0.0
    if isinstance(v, int | float) and not isinstance(v, bool) and v >= 0:
        return float(v)
    return allocation


def spent(records: list[Record], contract_sha256: str) -> float:
    reservations = [
        r for r in records if r.kind == "reserve" and r.data.get("contract") == contract_sha256
    ]
    accounts = {_key(r.data): r.data for r in records if r.kind == "account"}
    total = 0.0
    for r in reservations:
        impl = float(r.data["implementer_usd"])
        rev = float(r.data["reviewer_usd"])
        acct = accounts.get(_key(r.data))
        if acct is None:
            total += impl + rev
        else:
            total += _cost(acct.get("implementer_cost_usd"), impl)
            total += _cost(acct.get("reviewer_cost_usd"), rev)
    return total


def start_problems(
    records: list[Record], contract_sha256: str, approved_at: datetime, cfg: Config
) -> list[str]:
    """Why a new attempt may not start. Empty means it may (and must reserve first)."""
    problems = budget_problems(cfg.budget)
    if problems:
        return problems
    assert cfg.budget is not None
    # one attempt per approval: a rerun, a duplicate push or a retry after a
    # failure all need the owner to approve again, on top of the budget
    if any(
        r.kind == "reserve" and r.data.get("contract") == contract_sha256 and r.at > approved_at
        for r in records
    ):
        problems.append("this approval has already been used; re-apply the label to approve again")
    used = spent(records, contract_sha256)
    need = cfg.budget.implementer_usd + cfg.budget.reviewer_usd
    if cfg.budget.task_usd - used < need:
        problems.append(
            f"task budget exhausted: ${used:.2f} of ${cfg.budget.task_usd:.2f} counted, "
            f"an attempt needs ${need:.2f}"
        )
    return problems


def first_after(
    records: list[Record], contract_sha256: str, approved_at: datetime
) -> Record | None:
    """The reservation that owns this approval. Two runs that both passed
    start_problems before either wrote (pilot/N and pilot-drill/N don't share a
    concurrency group) resolve here: the earliest wins, the other refuses, and
    both still count as spent."""
    candidates = [
        r
        for r in records
        if r.kind == "reserve" and r.data.get("contract") == contract_sha256 and r.at > approved_at
    ]
    return min(candidates, key=lambda r: (r.at, r.id), default=None)


def owns(record: Record | None, run_id: str, attempt: str, base: str) -> bool:
    return (
        record is not None
        and _key(record.data) == (run_id, attempt)
        and record.data.get("base") == base
    )


def reservation(cfg: Config, contract_sha256: str, base: str, run_id: str, attempt: str) -> str:
    assert cfg.budget is not None
    return render(
        {
            "type": "reserve",
            "contract": contract_sha256,
            "base": base,
            "run_id": run_id,
            "run_attempt": attempt,
            "implementer_usd": cfg.budget.implementer_usd,
            "reviewer_usd": cfg.budget.reviewer_usd,
        }
    )


def reserved(
    records: list[Record], contract_sha256: str, base: str, run_id: str, attempt: str
) -> bool:
    """A model may run only under a reservation for exactly this run attempt.
    A GitHub "re-run job" is a new attempt with no reservation, so it refuses."""
    return any(
        r.kind == "reserve"
        and r.data.get("contract") == contract_sha256
        and r.data.get("base") == base
        and _key(r.data) == (run_id, attempt)
        for r in records
    )


def account(
    run_id: str, attempt: str, implementer_cost: float | None, reviewer_cost: float | str | None
) -> str:
    return render(
        {
            "type": "account",
            "run_id": run_id,
            "run_attempt": attempt,
            "implementer_cost_usd": implementer_cost,
            "reviewer_cost_usd": reviewer_cost,
        }
    )
