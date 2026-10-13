#!/usr/bin/env bash
#
# "Is the repo green?" — one command, same gates as CI.
#
#     ./scripts/verify.sh            # or: bash scripts/verify.sh
#
# Every setting lives in pyproject.toml (ruff, mypy, coverage floor), so this
# script only chooses the order and fails fast. It defines no thresholds of its
# own: there is nothing here to drift from CI's numbers because there are no
# numbers here.
#
# Step 4 runs the whole suite, which includes the Streamlit AppTest console
# suite (tests/test_dashboard_app.py). That is the project's non-interactive
# dashboard validation — it executes every view on a real ScriptRunner, which is
# the only way to catch a page that serves HTTP 200 and raises on interaction.
#
# Steps 5-6 are the end-to-end smoke: seed the deterministic demo from the
# committed fixtures, then drive the CLI over it. Credential-free and
# reproducible, which is what makes it cheap enough to run every time. Note that
# step 5 rebuilds data/agent_control_plane.db in place — deterministic, but it
# will replace whatever you had there. Set ACP_DB_PATH to point it elsewhere.
#
# CI (.github/workflows/ci.yml) deliberately keeps these as separate named steps
# rather than calling this script: GitHub annotates the failing step by name, and
# "Types" failing reads better than "verify.sh exited 1". The duplication is a
# list of command names; the configuration behind them is shared.

set -euo pipefail

cd "$(dirname "$0")/.."

# Prefer the project venv, whichever platform laid it out, so this works from a
# bare shell with no activation. PYTHON=... overrides.
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x .venv/bin/python ]; then
    PY=.venv/bin/python
  elif [ -x .venv/Scripts/python.exe ]; then
    PY=.venv/Scripts/python.exe
  else
    PY=python
  fi
fi

step() {
  printf '\n\033[1m==> %s\033[0m\n' "$1"
}

step "Interpreter"
"$PY" --version

step "Format (ruff format --check)"
"$PY" -m ruff format --check .

step "Lint (ruff check)"
"$PY" -m ruff check .

step "Types (mypy --strict: src + tests + scripts)"
"$PY" -m mypy --strict

step "Tests (pytest --cov, includes the Streamlit AppTest console suite)"
"$PY" -m pytest --cov

step "Demo seed (deterministic, from committed fixtures)"
"$PY" scripts/seed_demo.py --reset

step "CLI smoke"
ACP="$PY -m agent_control_plane.cli"
$ACP stats
$ACP runs
$ACP violations
$ACP trace mcp-analyst-2026-08-06
$ACP evaluate

# Proves the packaging entry point resolves, not just the module. Looks beside
# the interpreter first, since an unactivated venv puts `acp` out of PATH.
# Skipped rather than failed when the project is not installed here at all.
BIN="$(dirname "$PY")"
for candidate in "$BIN/acp" "$BIN/acp.exe" acp; do
  if command -v "$candidate" >/dev/null 2>&1; then
    step "Console script ($candidate)"
    "$candidate" --help >/dev/null
    break
  fi
done

printf '\n\033[1;32m==> green\033[0m\n'
