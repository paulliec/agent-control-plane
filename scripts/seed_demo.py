"""Generate the demo dataset. Thin wrapper so `python scripts/seed_demo.py` works
without installing the console script."""

from __future__ import annotations

import sys

from agent_control_plane.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["seed", *sys.argv[1:]]))
