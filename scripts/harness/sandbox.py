"""Run one command over a candidate tree inside a disposable container.

The isolation is the container's, not this module's: `--network none` leaves
only loopback, the environment is built here from constants (no host value is
ever forwarded), and the only mounts are the candidate tree and a read-only
wheelhouse built from the pinned base. Dependencies install offline into a
venv on tmpfs, so nothing persists between runs and nothing is fetched.

The container runs as the invoking (non-root) uid so it can write the bind-
mounted tree it was given and nothing else.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from scripts.harness.config import Sandbox

VENV = "/tmp/venv"


def container_env() -> dict[str, str]:
    # constants only: a host value here would be a credential leak waiting to happen
    return {
        "HOME": "/tmp",
        "PATH": f"{VENV}/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHON": f"{VENV}/bin/python",  # verify.sh honours it
        "PYTHONPATH": "/work/src",  # the candidate's code, not the wheel's copy
        "ACP_SANDBOX": "1",
        "PIP_NO_INDEX": "1",
        "PIP_FIND_LINKS": "/wheels",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def argv(sb: Sandbox, work: Path, wheels: Path, cmd: list[str], user: str) -> list[str]:
    setup = f"python -m venv {VENV} && {VENV}/bin/pip install -q " + " ".join(
        shlex.quote(x) for x in sb.install
    )
    inner = f"{setup} && cd /work && exec {shlex.join(cmd)}"
    env_flags = [a for k, v in container_env().items() for a in ("--env", f"{k}={v}")]
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--user",
        user,
        "--cpus",
        sb.cpus,
        "--memory",
        sb.memory,
        "--memory-swap",
        sb.memory,
        "--pids-limit",
        str(sb.pids),
        "--read-only",
        # exec: compiled extensions in the venv must be mmap-able
        "--tmpfs",
        "/tmp:rw,exec,nosuid,nodev,size=3g",
        "--mount",
        f"type=bind,src={work},dst=/work",
        "--mount",
        f"type=bind,src={wheels},dst=/wheels,readonly",
        *env_flags,
        "--workdir",
        "/work",
        sb.image,
        "sh",
        "-c",
        inner,
    ]


def client_env() -> dict[str, str]:
    """What the docker CLI itself sees: enough to find the daemon, nothing else."""
    return {k: os.environ[k] for k in ("PATH", "HOME", "DOCKER_HOST") if k in os.environ}


def run(sb: Sandbox, work: Path, wheels: Path, cmd: list[str]) -> tuple[int, str]:
    if sys.platform == "win32":
        return 126, "the sandbox runs only on a POSIX runner; refusing to run candidate code"
    user = f"{os.getuid()}:{os.getgid()}"
    try:
        proc = subprocess.run(
            argv(sb, work, wheels, cmd, user),
            env=client_env(),
            capture_output=True,
            text=True,
            timeout=sb.timeout_s,
        )
    except FileNotFoundError:
        return 127, "docker not found: refusing to run candidate code without a sandbox"
    except subprocess.TimeoutExpired:
        return 124, f"sandbox timed out after {sb.timeout_s}s"
    return proc.returncode, proc.stdout + proc.stderr
