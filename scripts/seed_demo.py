"""Generate the demo dataset. Thin wrapper so `python scripts/seed_demo.py` works
without installing the console script.

One deliberate addition over a bare `acp seed`: the demo supplies a Snowflake
credit rate. Snowflake reports credits, not dollars, and the USD rate is
contractual — so the library and the CLI both default to leaving cost unpriced
rather than inventing a list price. A demo with no Snowflake cost would fail to
show cost attribution working across sources, so we opt in here, visibly, and
`acp seed` prints the rate as an assumption. Override with
`--snowflake-credit-rate`.
"""

from __future__ import annotations

import sys

from agent_control_plane.cli import main

# Snowflake Standard-edition list pricing is the usual back-of-envelope figure.
# It is a stand-in for your contract rate, not a measurement.
DEMO_CREDIT_RATE_USD = "3.00"

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not any(a.startswith("--snowflake-credit-rate") for a in argv):
        argv = [*argv, "--snowflake-credit-rate", DEMO_CREDIT_RATE_USD]
    raise SystemExit(main(["seed", *argv]))
