### Goal

Add a regression test that pins the public demo's documented headline numbers,
so a change to the seed, fixtures, adapters or rules that moves them fails the
build instead of silently making `docs/CURRENT_STATE.md` wrong.

Documented values (`docs/CURRENT_STATE.md`, "Demo sources" and "Baseline"):
166 events in total — 143 `synthetic`, 10 `mcp`, 13 `snowflake` — and 20
violations after evaluation.

### Approved work item

1. Public demo readiness / deployment

### Allowed paths

- tests/test_demo_counts.py

### Acceptance criteria

- verify
- pytest tests/test_demo_counts.py
- manual: expected values are literals copied from docs/CURRENT_STATE.md, not computed by calling the code under test
- manual: the demo is seeded into a tmp_path database through the existing seed path, never data/agent_control_plane.db
- manual: per-source counts, the total and the violation count are each asserted separately, so a failure names which number moved

### Stop conditions

- The seeded numbers differ from the documented ones. Report both sets of
  values and stop. Do not change application code, fixtures, config, the seed
  or the documentation to make them agree.
- Pinning the numbers needs any path other than tests/test_demo_counts.py.
- `verify` is red for a reason unrelated to the new test.

### Out of scope

- Updating docs/CURRENT_STATE.md (the harness protects docs; Paul updates it)
- Pinning would_block or per-rule counts, which are not documented
- Refactoring existing tests or fixtures
