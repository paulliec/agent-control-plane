"""Generate the demo dataset. Thin wrapper so `python scripts/seed_demo.py` works
without installing the console script.

One deliberate addition over a bare `acp seed`: the demo supplies a Snowflake
credit rate. Snowflake reports credits, not dollars, and the USD rate is
contractual — so the library and the CLI both default to leaving cost unpriced
rather than inventing a list price. A demo with no Snowflake cost would fail to
show cost attribution working across sources, so we opt in here, visibly, and
`acp seed` prints the rate as an assumption. Override with
`--snowflake-credit-rate`.

The rate itself lives in `demo.py` alongside the rest of the demo's definition,
so the hosted console's self-seeding and this script price credits identically.
"""

from __future__ import annotations

import sys

from agent_control_plane.cli import main
from agent_control_plane.demo import DEMO_CREDIT_RATE_USD

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not any(a.startswith("--snowflake-credit-rate") for a in argv):
        argv = [*argv, "--snowflake-credit-rate", str(DEMO_CREDIT_RATE_USD)]
    raise SystemExit(main(["seed", *argv]))
