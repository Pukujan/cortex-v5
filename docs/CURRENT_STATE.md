# Cortex V5 Current State

**Last reconciled:** 2026-08-14  
**Repository:** `Pukujan/cortex-v5`  
**Accepted runtime baseline:** `31fde7508b8e1caddfe7f9b79dc5719c1a0df79f`

## Status

Cortex V5 is the active execution runtime. The first-real acceptance gate is complete.

Issue #2 is **HUMAN PASS / CLOSED COMPLETED**. The mechanical acceptance path used a public HumanEval item through the actual V5 HTTP API and recorded:

- executable verification `20/20`;
- `attempt_count=1`;
- local observation;
- Gravebuster HTTP 200;
- Langfuse HTTP 207.

The later owner declaration supplied the required human PASS.

## Supersession boundary

Cortex V4 is preserved/frozen historical implementation evidence. It is not active runtime authority and must not be revived merely because a historical task, PR, or test references it.

V5 deliberately owns its runtime state, methodology, seating/routing, task journal, attempts, receipts, tool containment, verification, and observability integrations without a V4/SSC runtime dependency.

## Cross-project authority

Current cross-project coordination is intentionally outside this repository:

- `Pukujan/fossil-core#86` — architecture authority;
- `Pukujan/fossil-core#94` — execution queue, claims, and closeouts.

Always read those live before treating a task name below as current.

## Post-acceptance integration

At the 2026-08-14 reconciliation, `CORTEX-02` — secretless GitHub Actions WorkOrder wiring — became eligible after V5 acceptance. It is a separate integration item, not evidence that V5 runtime acceptance is incomplete.

Any WorkOrder wiring must preserve:

- human authority;
- exact task scope;
- deterministic acceptance;
- distinct retry/model-switch attempts;
- workspace-contained tools;
- secretless ordinary Actions execution;
- no automatic production authority;
- fail-closed handling of empty/malformed/zero-usable model output.

## Transport boundary

LiteLLM/CKFF owns provider/model/route/capability/timeout/health transport facts. V5 owns execution policy and semantic task acceptance.

A transport `2xx` is not enough. Empty/malformed output, a completed stream with zero usable payload, or zero usable content/tool calls where required is failure.

## Acceptance is not deployment authority

The completed V5 acceptance does not authorize Railway deployment, production mutation, secret access, or sensitive-data routing. Those require separate explicit human-approved gates.

## Continuation

Read `../AGENTS.md`, the live FOSSIL #86/#94 state, and the focused task issue/PR. Do not infer current work solely from this snapshot.
