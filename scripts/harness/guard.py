"""PreToolUse hook for unattended runs. Reads the hook payload on stdin; exit 2
blocks the call and hands the reason back to the model.

Second layer behind the permission rules in .claude/harness/settings.json, not
a replacement for them, and not a sandbox: an allowed interpreter can still do
anything the process can. Path checks on file and search tools are exact. Git
is parsed and held to an allowlist; other shell checks are patterns. The
verifier re-checks the actual commits afterwards regardless.

Fails closed: an unreadable payload, an unparseable command or any crash
blocks the call.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

from scripts.harness import config, scope

FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
SEARCH_TOOLS = {"Grep", "Glob"}
SHELL_TOOLS = {"Bash", "PowerShell"}
NETWORK_TOOLS = {"WebFetch", "WebSearch"}

# What an implementer needs from git, and nothing that moves branches, rewrites
# history, touches config or remotes, or publishes.
GIT_SUBCOMMANDS = {"status", "diff", "log", "show", "add", "commit", "rev-parse", "ls-files"}
GIT_HARMLESS_GLOBALS = {"--no-pager", "-P"}

SHELL_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(p, re.IGNORECASE), why)
    for p, why in [
        (r"--no-verify\b", "skipping hooks"),
        (r"\bgh\b", "the agent does not talk to GitHub"),
        (
            r"\b(curl|wget|ssh|scp|rsync|ncat|nc|invoke-webrequest|invoke-restmethod|iwr|irm)\b",
            "no network",
        ),
        (r"\b(pip3?|uv|poetry|conda|npm|npx|pnpm|yarn)\b", "no package installs"),
        (r"\brm\s+-[a-z]*r|remove-item\b.*-recurse", "no recursive deletes"),
        (r"\bsudo\b|\bchmod\b|\bchown\b|\bicacls\b", "no permission changes"),
        # a nested shell or eval hides its command from everything below
        (r"\b(ba|z|da|k)?sh\s+-c\b|\beval\b|\bexec\b", "no nested shells"),
        # command position only, so `cat CLAUDE.md` still works
        (r"(^|[;&|(]\s*)claude(\s|$)", "no nested agent sessions"),
        (r"\.env\b", "secrets files are off limits"),
    ]
]


def _rel(path_str: str, root: Path) -> str | None:
    p = Path(path_str)
    resolved = (p if p.is_absolute() else root / p).resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def _segments(cmd: str) -> list[list[str]]:
    """Split a command line into simple commands. Raises ValueError if unparseable."""
    lexer = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    for tok in lexer:
        if tok and all(ch in ";&|()" for ch in tok):
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _git_problem(args: list[str]) -> str | None:
    """`args` follows the git executable. None if the invocation is allowed."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        # -C, -c, --git-dir, --work-tree and friends retarget git; nothing needs them
        if args[i] not in GIT_HARMLESS_GLOBALS:
            return f"git global option {args[i]!r}"
        i += 1
    if i == len(args):
        return None
    sub, rest = args[i], args[i + 1 :]
    if sub not in GIT_SUBCOMMANDS:
        return f"git {sub} is not allowed"
    if any(a.startswith("--output") for a in rest):
        return "git --output writes arbitrary files"
    if sub == "commit" and any(a == "-n" or a.startswith("--no-verify") for a in rest):
        return "skipping hooks"
    return None


def _shell_problem(cmd: str, cfg: config.Config, branch: str) -> str | None:
    for pattern, why in SHELL_RULES:
        if pattern.search(cmd):
            return why
    try:
        segments = _segments(cmd)
    except ValueError:
        return "unparseable command"
    for seg in segments:
        for i, tok in enumerate(seg):
            # catches /usr/bin/git, git.exe, and `env git …`
            if tok.replace("\\", "/").rsplit("/", 1)[-1].lower() in {"git", "git.exe"}:
                problem = _git_problem(seg[i + 1 :])
                if problem:
                    return problem
                sub = next((a for a in seg[i + 1 :] if not a.startswith("-")), None)
                if sub == "commit" and not branch.startswith(cfg.branch_prefix):
                    return f"commits only on {cfg.branch_prefix}* branches, not {branch!r}"
                break
    return None


def decide(
    tool: str, tool_input: dict[str, Any], cfg: config.Config, root: Path, branch: str
) -> str | None:
    """Reason to block, or None to let the permission rules decide."""
    if tool in NETWORK_TOOLS:
        return f"{tool}: no network access in unattended runs"

    if tool in SEARCH_TOOLS:
        path = str(tool_input.get("path") or ".")
        if _rel(path, root) is None:
            return f"{tool}: {path!r} is outside the repository"
        # a Glob pattern can point elsewhere on its own, whatever `path` says
        pattern = str(tool_input.get("pattern", ""))
        if tool == "Glob" and (
            Path(pattern).is_absolute()
            # drive paths, whichever OS the guard happens to run on
            or re.match(r"^[A-Za-z]:", pattern)
            or pattern.startswith(("/", "\\", "~"))
            or ".." in re.split(r"[\\/]", pattern)
        ):
            return f"Glob: pattern {pattern!r} reaches outside the repository"
        return None

    if tool in FILE_TOOLS or tool == "Read":
        target = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        rel = _rel(str(target), root)
        if rel is None:
            return f"{tool}: {target!r} is outside the repository"
        if tool == "Read":
            return (
                f"Read: {rel} is a secrets file" if rel.split("/")[-1].startswith(".env") else None
            )
        if cfg.is_protected(rel):
            return f"{tool}: {rel} is protected (harness.toml); stop and report instead"
        return None

    if tool in SHELL_TOOLS:
        cmd = str(tool_input.get("command", ""))
        problem = _shell_problem(cmd, cfg, branch)
        if problem:
            return f"{tool}: blocked ({problem}): {cmd[:200]}"
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        cfg = config.load()
        reason = decide(
            payload.get("tool_name", ""),
            payload.get("tool_input") or {},
            cfg,
            config.REPO_ROOT,
            scope.current_branch(config.REPO_ROOT),
        )
    except Exception as e:  # fail closed
        print(f"harness guard failed, blocking: {e!r}", file=sys.stderr)
        return 2
    if reason:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
