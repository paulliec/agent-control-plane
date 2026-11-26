#!/usr/bin/env bash
# Engine adapter: Claude Code, headless. The only engine-specific code in the
# pilot; another engine is another file with the same arguments.
#
#   claude.sh ROLE WORKDIR PROMPT OUT TURNS BUDGET_USD TOOLS SETTINGS [SCHEMA]
#
# Never --bare: it skips hooks, and the guard is a hook. Settings come from the
# trusted checkout (SETTINGS), not the working copy the model edits. TOOLS is the
# whole built-in toolset; the caller decides whether a shell exists at all.
set -euo pipefail

role=$1 workdir=$2 prompt=$3 out=$4 turns=$5 budget=$6 tools=$7 settings=$8 schema=${9:-}

args=(
  -p --agent "$role"
  --settings "$settings" --setting-sources project
  --permission-mode dontAsk --permission-prompts none
  --tools "$tools"
  --max-turns "$turns" --max-budget-usd "$budget"
  --no-session-persistence --output-format json
)
if [ -n "$schema" ]; then
  args+=(--json-schema "$(cat "$schema")")
fi

cd "$workdir"
# a non-zero exit (budget or turns exhausted) still leaves run.json for accounting
claude "${args[@]}" < "$prompt" > "$out" || exit $?
