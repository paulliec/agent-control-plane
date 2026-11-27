"""What may leave a job: secret detection and redaction, applied before upload.

Two kinds of secret are recognised: anything shaped like a credential
(gate.SECRET_PATTERNS, which includes the drill canary), and the literal values
of credentials this very job holds. The second catches a key with no
recognisable shape, which a pattern alone would let through.
"""

from __future__ import annotations

import os
from typing import Any

from scripts.harness.gate import SECRET_PATTERNS

# credentials a pilot job can hold; their values must never be written out
HELD = (
    "ANTHROPIC_API_KEY",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
)
REDACTED = "[REDACTED]"


def held_values(env: dict[str, str] | None = None) -> list[str]:
    e = env if env is not None else dict(os.environ)
    # short values would redact half the alphabet; real credentials are long
    return [v for k in HELD if len(v := e.get(k, "")) >= 12]


def findings(text: str, env: dict[str, str] | None = None) -> list[str]:
    """Names of what was found, never the values."""
    hits = sorted(name for name, rx in SECRET_PATTERNS.items() if rx.search(text))
    if any(v in text for v in held_values(env)):
        hits.append("held-credential")
    return hits


def redact(text: str, env: dict[str, str] | None = None) -> str:
    for v in held_values(env):
        text = text.replace(v, REDACTED)
    for rx in SECRET_PATTERNS.values():
        text = rx.sub(REDACTED, text)
    return text


def redact_obj(obj: Any, env: dict[str, str] | None = None) -> Any:
    if isinstance(obj, str):
        return redact(obj, env)
    if isinstance(obj, dict):
        return {k: redact_obj(v, env) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, env) for v in obj]
    return obj
