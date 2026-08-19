# Cortex V5 Agent Continuation Contract

Use this file when continuing Cortex V5 autonomously. Do not rely on chat memory as project state.

## Read first

1. `README.md`
2. `docs/CURRENT_STATE.md`
3. Cortex V5 Issue #2 for the completed first-real acceptance contract/history
4. `Pukujan/fossil-core#86` for current cross-project architecture authority
5. latest `Pukujan/fossil-core#94` comments for live task/claim/closeout state
6. the focused V5 issue/PR for the task actually being worked

Re-fetch live GitHub state before any mutation. Repository prose must not become a competing queue.

## Current state — 2026-08-14

- Cortex V5 is the active execution runtime.
- Accepted baseline: `31fde7508b8e1caddfe7f9b79dc5719c1a0df79f`.
- `V5-ACCEPTANCE` is HUMAN PASS / CLOSED COMPLETED.
- Cortex V4 is frozen historical evidence, not runtime authority.
- Cross-project architecture authority is FOSSIL #86; coordination/claims live in FOSSIL #94.
- `CORTEX-02` secretless Actions WorkOrder wiring is a separate post-acceptance integration item. Verify its live #94 state before claiming it.

## Authority rules

- The human is the only final authority.
- A model saying “done” is never a completion gate.
- Deterministic verification must pass exactly as specified.
- An agent may not widen scope, access class, acceptance criteria, or production authority.
- Retry/model-switch attempts remain distinct attempts with distinct receipts.
- An adjudicator recommendation cannot override a failed deterministic checker.
- LiteLLM/CKFF supplies transport facts; Cortex owns execution policy.
- `2xx` with empty, malformed, or zero-usable output is failure, not success.

## Runtime boundary

Do not reintroduce runtime dependencies on Cortex V4 or SSC.

V5 owns its own:

- methodology catalog;
- routing/seating policy;
- task state and journal;
- attempts/receipts;
- tool containment;
- deterministic verification;
- observability adapters;
- model outcome bookkeeping.

Historical V4 code/tests can be evidence when explicitly re-evaluated, but they are not current runtime authority by default.

## Engineering policy

For deterministic code behavior:

1. **RED** — failed-first test/probe.
2. **GREEN** — smallest bounded implementation.
3. **REGRESSION** — targeted neighbors + full suite.
4. **CLEAN VERIFY** — fresh environment/worktree where practical.
5. **HOSTED EVIDENCE** — exact-head CI when the task uses hosted acceptance.

Use SDD for every material change and TDD where deterministic behavior is testable. Add regression coverage for discovered bugs. Do not skip, xfail, loosen, suppress, or narrow a gate merely to obtain green.

## Security / tool rules

- Tools must remain workspace-contained and validated.
- Do not expose `.env`, API keys, bearer tokens, cookies, telemetry credentials, or production secrets to model output/logs/Git.
- Secret-bearing execution requires an explicitly authorized lane; role names do not grant secrets.
- Ordinary GitHub/PR work should remain secretless.
- No production deployment follows from acceptance, a WorkOrder, or an agent closeout.

## Acceptance history

The completed first-real acceptance used a public HumanEval item through the actual V5 HTTP API with executable tests as the gate. Recorded mechanical evidence was `20/20`, one attempt, with required local, Gravebuster, and Langfuse observation; the owner then supplied human PASS.

Do not replace future real-acceptance claims with fixtures or simulated model responses.

## Claim / continuation behavior

When work is coordinated through the FOSSIL ledger:

1. inspect live #94;
2. identify an eligible task and exact starting ref;
3. claim before mutation when the ledger requires it;
4. re-fetch to confirm the winning claim;
5. work on an isolated branch/worktree;
6. report exact SHA/tests/hosted evidence;
7. never self-infer a merge, issue close, production promotion, or human PASS.

If no task is eligible, stop with explicit BLOCKED/idle evidence rather than inventing work.
