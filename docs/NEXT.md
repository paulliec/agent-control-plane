# Next

What is approved to build, and what is not. This file exists so an autonomous
coding session cannot decide the product roadmap by itself.

If a task is not on the approved list, **stop and ask** — even when the code
obviously wants it, even when it is a small change, and even when something in
`CURRENT_STATE.md` is labelled a limitation. A documented limitation is a
decision, not a backlog ticket.

## Current approved work

1. **Public demo readiness / deployment.** Making the existing Streamlit
   application safe and simple to run as a public, deterministic demo. No new
   product features.
2. **Code-review / demo documentation.** README as a reviewer's entry point, the
   harness files (`CLAUDE.md`, `CURRENT_STATE.md`, this file, `scripts/verify.sh`),
   and the safety scrub that makes the repo publishable.
3. **Development harness, Phase 0/1.5.** Task contracts and eligibility
   checks, implementer and read-only reviewer agents, the trusted verifier and
   protected-path checks, unattended permission settings, the Phase 1.5
   security fixes, and their documentation (`docs/HARNESS.md`). Branch
   `feat/autonomous-harness` only. Not approved within this item: scheduled or
   unattended runs, any further publishing or PRs, changes to GitHub repository
   settings, and any change to application logic.

   Two narrow exceptions were each approved separately by Paul:
   - **Publishing:** pushing `demo-release-readiness` and
     `feat/autonomous-harness`, and opening draft PR #1 against
     `demo-release-readiness` (2026-11-15). Nothing else is published.
   - **CI token hardening (`e322bc4`):** in `.github/workflows/ci.yml`, a
     workflow-level `permissions: contents: read` and `persist-credentials: false`
     on checkout, pinned by `test_ci_token_is_read_only_and_not_persisted`. No
     verification command, trigger or threshold changed. Any other CI change
     remains outside this item.

Tests for new deterministic behaviour are in scope. Lowering an existing gate to
make something fit is not.

## Not approved yet

Designed, discussed or obvious — none of it authorized:

- Harness Phase 2+: headless runner, scheduled or hosted runs, GitHub writes
  (labels, comments, branches, PRs)
- Tollgate 3, in whole or in part
- Agent discovery / inventory
- Declared-vs-observed reconciliation
- Live Snowflake extraction (connector, credentials, polling)
- Azure AI Foundry adapter
- Databricks adapter
- Active enforcement or inline blocking
- Ruleset versioning
- Retention policy
- Unknown-agent model redesign, including the `AgentEvent.agent_id` sentinel
- Cost provenance redesign
- New policy rules
- Auth / authz, multi-user SaaS infrastructure
- Real-time polling or streaming ingest
- New dashboard features unrelated to public-demo readiness
- Swapping the persistence store or the frontend framework

## Architectural non-negotiables

Do not change the semantics of any of these as a side effect of approved work:

`AgentEvent` · tenant isolation · event fingerprints · policy decisions ·
`unknown_data_domain` · `unauthorized_data_domain` · `unregistered_agent` ·
Snowflake native attribution · `QUERY_TAG` attribution · `actor_id` vs
`agent_id` · Snowflake multi-object fanout · Snowflake multi-root execution
chains · cost calculation and provenance · MCP mappings · dashboard filtering
and drill-downs.

## When to stop and report

Rather than making a broad call alone:

- Approved work would require changing core persistence semantics
- Publishing would expose a real secret or private dataset
- Deployment would need an architecture materially different from the local app
- Deployment would need a production auth system
- Existing seed logic cannot be safely reused
- CI and local verification disagree in a material way

Report: what you found, why it matters, the smallest options, your
recommendation.
