"""SQLite-backed control plane store.

SQLite because it ships with Python, is transactional, and the MVP's query
volume is trivial. When event volume or column-scan analytics outgrow it,
`ControlPlaneRepository` is the seam — a DuckDB implementation drops in behind
the same Protocol. See docs/ARCHITECTURE.md.

Tenancy: `tenant_id` is part of both primary keys and every index, and every
read method requires it. There is no query path that returns rows for more than
one tenant except `list_tenants()`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent_control_plane.logging import get_logger, log_context
from agent_control_plane.models.event import AgentEvent, ExecutionChain, build_chain
from agent_control_plane.models.policy import PolicyResult

log = get_logger(__name__)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    tenant_id          TEXT NOT NULL,
    event_id           TEXT NOT NULL,
    timestamp          TEXT NOT NULL,
    platform           TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    agent_name         TEXT,
    actor_id           TEXT,
    actor_type         TEXT,
    event_type         TEXT NOT NULL,
    action             TEXT NOT NULL,
    tool_name          TEXT,
    tool_server        TEXT,
    resource           TEXT,
    data_domain        TEXT,
    status             TEXT NOT NULL,
    error_code         TEXT,
    error_message      TEXT,
    duration_ms        INTEGER,
    estimated_cost_usd REAL,
    model              TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    parent_event_id    TEXT,
    correlation_id     TEXT NOT NULL,
    session_id         TEXT,
    source_adapter     TEXT NOT NULL,
    ingested_at        TEXT,
    metadata           TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (tenant_id, event_id)
);

CREATE INDEX IF NOT EXISTS idx_events_correlation ON events(tenant_id, correlation_id);
CREATE INDEX IF NOT EXISTS idx_events_agent       ON events(tenant_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp   ON events(tenant_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_parent      ON events(tenant_id, parent_event_id);

CREATE TABLE IF NOT EXISTS policy_results (
    tenant_id      TEXT NOT NULL,
    result_id      TEXT NOT NULL,
    policy_name    TEXT NOT NULL,
    severity       TEXT NOT NULL,
    passed         INTEGER NOT NULL,
    decision       TEXT NOT NULL DEFAULT 'allow',
    enforced       INTEGER NOT NULL DEFAULT 0,
    correlation_id TEXT NOT NULL,
    event_id       TEXT,
    agent_id       TEXT,
    explanation    TEXT NOT NULL,
    evidence       TEXT NOT NULL DEFAULT '{}',
    evaluated_at   TEXT NOT NULL,
    PRIMARY KEY (tenant_id, result_id)
);

CREATE INDEX IF NOT EXISTS idx_policy_correlation ON policy_results(tenant_id, correlation_id);
CREATE INDEX IF NOT EXISTS idx_policy_passed      ON policy_results(tenant_id, passed);
"""

_EVENT_COLUMNS = [
    "tenant_id",
    "event_id",
    "timestamp",
    "platform",
    "agent_id",
    "agent_name",
    "actor_id",
    "actor_type",
    "event_type",
    "action",
    "tool_name",
    "tool_server",
    "resource",
    "data_domain",
    "status",
    "error_code",
    "error_message",
    "duration_ms",
    "estimated_cost_usd",
    "model",
    "input_tokens",
    "output_tokens",
    "parent_event_id",
    "correlation_id",
    "session_id",
    "source_adapter",
    "ingested_at",
    "metadata",
]

_POLICY_COLUMNS = [
    "tenant_id",
    "result_id",
    "policy_name",
    "severity",
    "passed",
    "decision",
    "enforced",
    "correlation_id",
    "event_id",
    "agent_id",
    "explanation",
    "evidence",
    "evaluated_at",
]


class SchemaVersionError(RuntimeError):
    """The database on disk was written by a different schema version."""


class SQLiteRepository:
    """Implements `ControlPlaneRepository`.

    Not thread-safe by design: one connection per process/worker. Streamlit gets
    its own instance per session.
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle --------------------------------------------------------

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            if str(self.db_path) != ":memory:":
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def initialise(self) -> None:
        existing = self._stored_schema_version()
        if existing is not None and existing != SCHEMA_VERSION:
            # No migration tooling yet, and silently reading a v1 database with
            # v2 code would return rows missing tenant_id. Fail loudly instead.
            raise SchemaVersionError(
                f"{self.db_path} is schema v{existing}, this build expects "
                f"v{SCHEMA_VERSION}. Delete it and re-seed, or add a migration."
            )
        with self.conn:
            self.conn.executescript(_SCHEMA)
            self.conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
        log.debug("schema ready", extra=log_context(db=str(self.db_path)))

    def _stored_schema_version(self) -> int | None:
        tables = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if tables is None:
            return None
        row = self.conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"]) if row else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> SQLiteRepository:
        self.initialise()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- events -----------------------------------------------------------

    def save_events(self, events: list[AgentEvent]) -> int:
        if not events:
            return 0
        placeholders = ", ".join("?" * len(_EVENT_COLUMNS))
        sql = f"INSERT OR IGNORE INTO events ({', '.join(_EVENT_COLUMNS)}) VALUES ({placeholders})"
        tenants = {e.tenant_id for e in events}
        before = sum(self.count_events(t) for t in tenants)
        with self.conn:
            self.conn.executemany(sql, [_event_to_row(e) for e in events])
        written = sum(self.count_events(t) for t in tenants) - before
        if written != len(events):
            log.info(
                "skipped duplicate events",
                extra=log_context(submitted=len(events), written=written),
            )
        return written

    def get_event(self, tenant_id: str, event_id: str) -> AgentEvent | None:
        row = self.conn.execute(
            "SELECT * FROM events WHERE tenant_id = ? AND event_id = ?",
            (tenant_id, event_id),
        ).fetchone()
        return _row_to_event(row) if row else None

    def list_events(
        self,
        tenant_id: str,
        *,
        correlation_id: str | None = None,
        agent_id: str | None = None,
        platform: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[AgentEvent]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        for column, value in (
            ("correlation_id", correlation_id),
            ("agent_id", agent_id),
            ("platform", platform),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(str(value))
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(_iso(since))
        if until is not None:
            clauses.append("timestamp <= ?")
            params.append(_iso(until))

        sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)}"
        sql += " ORDER BY timestamp ASC, event_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_event(r) for r in self.conn.execute(sql, params)]

    def list_correlation_ids(self, tenant_id: str, limit: int | None = None) -> list[str]:
        sql = (
            "SELECT correlation_id, MAX(timestamp) AS last_seen FROM events "
            "WHERE tenant_id = ? GROUP BY correlation_id ORDER BY last_seen DESC"
        )
        params: list[Any] = [tenant_id]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [str(r["correlation_id"]) for r in self.conn.execute(sql, params)]

    def get_chain(self, tenant_id: str, correlation_id: str) -> ExecutionChain | None:
        events = self.list_events(tenant_id, correlation_id=correlation_id)
        if not events:
            return None
        return build_chain(correlation_id, events, tenant_id=tenant_id)

    def count_events(self, tenant_id: str) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
        return int(row["n"])

    def list_tenants(self) -> list[str]:
        return [
            str(r["tenant_id"])
            for r in self.conn.execute("SELECT DISTINCT tenant_id FROM events ORDER BY tenant_id")
        ]

    # -- policy results ---------------------------------------------------

    def save_policy_results(self, results: list[PolicyResult]) -> int:
        if not results:
            return 0
        placeholders = ", ".join("?" * len(_POLICY_COLUMNS))
        sql = (
            f"INSERT OR REPLACE INTO policy_results ({', '.join(_POLICY_COLUMNS)}) "
            f"VALUES ({placeholders})"
        )
        with self.conn:
            self.conn.executemany(sql, [_policy_to_row(r) for r in results])
        return len(results)

    def list_policy_results(
        self,
        tenant_id: str,
        *,
        correlation_id: str | None = None,
        violations_only: bool = False,
        limit: int | None = None,
    ) -> list[PolicyResult]:
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if correlation_id is not None:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if violations_only:
            clauses.append("passed = 0")

        sql = f"SELECT * FROM policy_results WHERE {' AND '.join(clauses)}"
        sql += " ORDER BY evaluated_at DESC, result_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        return [_row_to_policy(r) for r in self.conn.execute(sql, params)]

    def delete_policy_results(self, tenant_id: str, correlation_id: str) -> int:
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM policy_results WHERE tenant_id = ? AND correlation_id = ?",
                (tenant_id, correlation_id),
            )
        return cur.rowcount


# -- row mapping ----------------------------------------------------------


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _event_to_row(event: AgentEvent) -> tuple[Any, ...]:
    data = event.model_dump()
    data["timestamp"] = _iso(event.timestamp)
    data["ingested_at"] = _iso(event.ingested_at) if event.ingested_at else None
    data["metadata"] = json.dumps(data["metadata"], default=str)
    return tuple(data[col] for col in _EVENT_COLUMNS)


def _row_to_event(row: sqlite3.Row) -> AgentEvent:
    data = {col: row[col] for col in _EVENT_COLUMNS}
    data["metadata"] = json.loads(data["metadata"] or "{}")
    return AgentEvent.model_validate(data)


def _policy_to_row(result: PolicyResult) -> tuple[Any, ...]:
    data = result.model_dump()
    data["passed"] = 1 if result.passed else 0
    data["enforced"] = 1 if result.enforced else 0
    data["evidence"] = json.dumps(data["evidence"], default=str)
    data["evaluated_at"] = _iso(result.evaluated_at)
    return tuple(data[col] for col in _POLICY_COLUMNS)


def _row_to_policy(row: sqlite3.Row) -> PolicyResult:
    data = {col: row[col] for col in _POLICY_COLUMNS}
    data["passed"] = bool(data["passed"])
    data["enforced"] = bool(data["enforced"])
    data["evidence"] = json.loads(data["evidence"] or "{}")
    return PolicyResult.model_validate(data)
