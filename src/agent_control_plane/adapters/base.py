"""Adapter contract.

An adapter's only job is: pull raw records from one source and emit
`AgentEvent`s. It must not touch the repository, the policy engine, or the
dashboard. Everything vendor-specific — auth, pagination, field names, cost
maths — stops here.

Adding Snowflake/Databricks/Foundry later means writing a new `EventSource`,
not touching the core.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from agent_control_plane.logging import get_logger, log_context
from agent_control_plane.models.event import AgentEvent

log = get_logger(__name__)


@runtime_checkable
class EventSource(Protocol):
    """A pullable source of agent activity."""

    @property
    def name(self) -> str:
        """Stable adapter id, written to `AgentEvent.source_adapter`."""
        ...

    def fetch(self, since: datetime | None = None) -> Iterable[AgentEvent]:
        """Yield normalized events, optionally only those after ``since``."""
        ...


class AdapterError(RuntimeError):
    """Raised when a source is unreachable or its payload is unusable."""


class NormalizationError(AdapterError):
    """A single raw record could not be mapped. Carries the record for triage."""

    def __init__(self, message: str, record: Any) -> None:
        super().__init__(message)
        self.record = record


def normalize_all(
    source_name: str,
    records: Iterable[Any],
    mapper: Any,
    *,
    strict: bool = False,
) -> Iterator[AgentEvent]:
    """Map raw records to events, isolating per-record failures.

    A malformed row in an audit export shouldn't sink the whole ingest run, so
    the default is skip-and-log. `strict=True` is for tests and for sources
    where silent loss is unacceptable.
    """
    for index, record in enumerate(records):
        try:
            yield mapper(record)
        except Exception as exc:  # adapter boundary: raw data is untrusted
            if strict:
                raise NormalizationError(f"{source_name} record {index}: {exc}", record) from exc
            log.warning(
                "dropped unmappable record",
                extra=log_context(adapter=source_name, index=index, error=str(exc)),
            )
