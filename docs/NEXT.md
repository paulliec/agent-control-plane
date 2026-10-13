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

Tests for new deterministic behaviour are in scope. Lowering an existing gate to
make something fit is not.

## Not approved yet

Designed, discussed or obvious — none of it authorized:

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
