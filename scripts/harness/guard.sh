#!/usr/bin/env bash
# PreToolUse hook entry point. Picks the interpreter the way verify.sh does and
# turns any failure into exit 2, because Claude Code treats other non-zero
# exits as a non-blocking error: a guard that crashed would otherwise allow.
cd "$(dirname "$0")/../.." || exit 2

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then
    PY=.venv/Scripts/python.exe
  else
    PY=python3
  fi
fi

"$PY" -m scripts.harness.guard || exit 2
