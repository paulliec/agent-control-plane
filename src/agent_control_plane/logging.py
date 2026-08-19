"""Structured logging without pulling in a logging framework.

Anything passed via ``extra={"context": {...}}`` is rendered as key=value (or
folded into the JSON record). That keeps ingestion/policy logs greppable and
machine-parseable without decorating every call site.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

_CONTEXT_KEY = "context"


class KeyValueFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = (
            f"{self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z')} {record.levelname:<7} "
            f"{record.name} {record.getMessage()}"
        )
        ctx = getattr(record, _CONTEXT_KEY, None)
        if isinstance(ctx, dict) and ctx:
            base += " " + " ".join(f"{k}={_scalar(v)}" for k, v in ctx.items())
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        ctx = getattr(record, _CONTEXT_KEY, None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def _scalar(value: Any) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    """Idempotent root configuration. Safe to call from CLI, tests, dashboard."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else KeyValueFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_context(**kwargs: Any) -> dict[str, Any]:
    """Sugar: ``logger.info("msg", extra=log_context(run=x))``."""
    return {_CONTEXT_KEY: kwargs}
