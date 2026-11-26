"""Scripted stand-in for a coding model: deterministic, no network, no cost.

Same arguments as claude.sh. AEH_FAKE_SCRIPT points at a JSON file:

    {"writes": {"relative/path": "content", ...},
     "cost_usd": 0.0 | null,
     "subtype": "success" | "error_max_turns" | "error_max_budget_usd" | ...,
     "is_error": false,
     "result": "the model's closing text",
     "raw_output": "written instead of JSON, to simulate a garbled result",
     "stderr": "printed to stderr",
     "structured_output": {...},          # reviewer role only
     "exit": 0}

It writes exactly what it is told, anywhere in the working copy. That models a
model that got past every in-session control, so drills exercise the controls
downstream of the engine. It also records which environment variable *names*
it received, so tests can check what reached it.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    role, workdir, _prompt, out = argv[0], Path(argv[1]), argv[2], Path(argv[3])
    script_path = os.environ.get("AEH_FAKE_SCRIPT")
    script = json.loads(Path(script_path).read_text(encoding="utf-8")) if script_path else {}
    for rel, content in (script.get("writes") or {}).items():
        target = workdir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    if "stderr" in script:
        print(script["stderr"], file=sys.stderr)
    if "raw_output" in script:
        out.write_text(script["raw_output"], encoding="utf-8")
        return int(script.get("exit", 0))
    result = {
        "engine": "fake",
        "role": role,
        "subtype": script.get("subtype", "success"),
        "is_error": script.get("is_error", False),
        "result": script.get("result", ""),
        "total_cost_usd": script.get("cost_usd", 0.0),
        "num_turns": 1,
        "env_names": sorted(os.environ),
    }
    if role == "reviewer" and "structured_output" in script:
        result["structured_output"] = script["structured_output"]
    out.write_text(json.dumps(result), encoding="utf-8")
    return int(script.get("exit", 0))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
