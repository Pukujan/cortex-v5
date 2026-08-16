# Cortex V5 methodology research handoff

Date: 2026-08-15

Purpose: continue the V5 methodology/agent-runtime research before changing code. This is a decision and issue log, not an implementation plan approved for execution.

## Confirmed owner decisions

1. **V5 must be standalone.**
   - V5 must copy/port the required SSC and V4 behavior into V5-owned modules and connect it there.
   - No runtime imports, adapters, or dependency on SSC or V4 are acceptable in the completed V5.

2. **Behavioral parity matters.**
   - Preserve the same named concepts, methodology IDs, seat concepts, tool-policy intent, and fallback semantics unless an explicit V5 replacement has been decided.
   - V4 is not the destination architecture. It dynamically imports SSC for core summon/runtime behavior and adds control layers. V5 must become the integrated successor.

3. **Two explicit substitutions from SSC/V4.**
   - Replace corpus reading with a task scratchpad.
   - Replace permanent closeout writing with a closeout scratchpad.
   - Working and closeout scratchpads are intended to be throwable after they are no longer needed.
   - This does **not** automatically apply to sealed holdouts or durable oracle material; their lifecycle remains an open decision.

4. **V5 already has intentional improvements.**
   - Granular task handling and timeout/retry control are V5 features to retain.
   - V5 seating is a new system; do not replace it with the old V4 fallback matrix merely because V4 is older.

5. **No success-history strength ranking.**
   - Benchmark/research tier order is the fixed strength prior.
   - Per-model successes/failures must not reshuffle model strength.
   - Runtime outcomes are for the existing retry/failure loops, described by the owner as the `3` and then `20` thresholds.

6. **Tool broker is required.**
   - Broker permissions must be driven by methodology seating / seat role.
   - The present V5 behavior of giving every model the same four file tools is not the intended final behavior.

## Canonical V5 model-tier order

Treat this as the exact fixed, benchmark-grounded `MODEL_TIERS` priority, highest first. Models absent from the list are below it and ordered deterministically.

1. `grok-4.6`
2. `gpt-5.6-sol`
3. `kimi-k3`
4. `qwen3.8-max`
5. `gemini-3.6-flash`
6. `gpt-5.5`
7. `gpt-5.6-terra`
8. `gemini-3.5-flash`
9. `gemini-3.5-flash-high`
10. `gpt-5.6-luna`
11. `glm-5.2`
12. `glm-5.2-metered`
13. `glm-5`
14. `minimax-m3`
15. `deepseek-v4-pro`
16. `deepseek-v4-flash`
17. `mimo-v2.5-pro`
18. `kimi-k2.7-code`
19. `kimi-k2-thinking`
20. `gemini-3.1-pro-preview`
21. `qwen3.6-plus`
22. `glm-5-turbo`
23. `minimax-m2.5`
24. `glm-4.7`
25. `deepseek-v3.2`
26. `gemini-3.1-flash-lite-preview`
27. `qwen3-coder-next`
28. `grok-4.5`
29. `qwen-3.6-max`
30. `gemini-3.1-pro-preview-search`
31. `gemini-3.5-flash-search`
32. `gemini-3.1-flash-lite-image`
33. `qwen3.7-flash`

The intended selection shape is currently understood as:

```text
live model catalog
→ methodology / task relevance
→ fixed MODEL_TIERS strength order
→ retry controls (not success-based re-ranking)
```

## Current implementation gaps

These are observations, not approval to change them.

### Seating

- GitHub `main` currently scores models as:

  ```text
  (available, tag_overlap, -tier, success - failure, success, model)
  ```

- Owner correction: remove the two success-history terms from rank ordering; strengths must remain the fixed tier order.
- Current tags are lexical overlap between model-name tokens and task/risk/routing tags. This is not yet a real methodology-role capability matrix.
- The retry controller currently has a three-probe-failure and twenty-continuous-failure mechanism. Exact desired action, reset, vendor-diversity behavior, and terminal state at each threshold still need confirmation.

### Tool broker and agent runtime

- Current V5 exposes `read`, `write`, `edit`, and `list` to every executing model.
- There is no methodology-seated broker, no per-role tool schema, and no implementation of M29 seat access control.
- Current V5 has a contained verifier-only command capability. It is intentionally not model-facing.
- Needed future question: which SSC/V4 tools and guards are ported, which are replaced by scratchpad behavior, and what policy grants each tool.

### Methodology is mostly declarative in V5

- `cortex_v5/methodology.py` contains local M0–M33 titles and classifier rules.
- Selecting `M3`, `M4`, or `M20` currently does not execute their full SSC procedures.
- V5 does have a deterministic `VerificationGate`, but it does not create independent test suites, protect sealed holdouts, run mutation testing, cross-validate oracles, or report oracle health.

### Multi-model work is not yet faithful to canonical M5/M5b

- Current V5 `MultiModelArbitrator` runs isolated candidate workspaces, checker-gates candidates, and may call an optional adjudicator that cannot override a failed checker.
- It is a reasonable candidate-patch/eval mechanism, but it is not the canonical M5 procedure merely because it has multiple models.

## Canonical methodology distinctions that must not be collapsed

### M3 — P4 build lane

For trust-critical modules:

```text
frozen contract
→ independent implementer
→ independent test author, blind to implementation
→ visible acceptance suite
→ convergence and reconciliation
→ M4 sealed holdout by a third agent
```

### M4 — sealed holdout verification

The third party writes new probes from the contract. The build agent must not see them. The procedure calls for negative/adversarial probes, double-run determinism, and targeted mutant killing.

### M5 — multi-model arbitration

```text
produce artifact/position
→ different-family independent critique, receiving artifact not original chat
→ third seat/orchestrator adjudicates disputed points from evidence
```

This is for disputed high-stakes judgment and adversarial artifact review. It is not simply “two builders vote.”

### M5b — cross-vendor blind convergence

Two different-vendor frontier models independently receive the same self-contained **high-stakes design decision**, blind to one another. Same verdict plus same core invariant is evidence; divergence is valuable and should reveal a real owner decision. The manual explicitly distinguishes M5b from code-diff selection.

### M20 — oracle minting

An oracle is not a model’s opinion. It is the deterministic, inspectable mechanism that converts observable behavior into a pass/fail/unverifiable verdict.

```text
written contract
→ explicit, observable success condition
→ executable checker / oracle
→ run system under test
→ PASS, FAIL, or UNVERIFIABLE
```

M20 asks for deterministic verdict paths, independent checker cross-validation, hidden anti-gaming probes, oracle audit on real data, versioning/freeze, and oracle-health emission.

## What “oracle” means for V5

- A contract states an expected behavior.
- A success condition makes the expected behavior observable.
- The oracle/checker implements the condition and returns a verdict.
- It does not cause the product to succeed; it observes whether it did.
- A vague condition such as “clean architecture” is not an oracle until decomposed into observable invariants.
- If the contract does not permit a reliable verdict, return `UNVERIFIABLE` rather than inventing a pass.

Example:

```text
Contract: scratchpad data is private per task and deleted after terminal closeout.

Oracle:
1. Create task A and task B.
2. Write secret marker into A scratchpad.
3. Assert B cannot read marker.
4. Complete A.
5. Assert A scratchpad is deleted.
```

## Research findings to carry forward

1. **The full SSC M3/M4/M20 stack is not externally validated as one package.**
   - Treat it as a credible high-assurance hypothesis, not as a proven production methodology for V5.
   - V5 should eventually measure it through an A/B/C evaluation rather than adopting it on faith.

2. **TDD has evidence, with tradeoffs.**
   - Systematic reviews report quality benefits in many studies, but productivity results are mixed and can be worse in industrial settings.
   - Source: https://www.sciencedirect.com/science/article/pii/S0950584916300222

3. **Independent implementations are not automatically independent.**
   - The Knight–Leveson N-version study found correlated failures among independently developed versions.
   - Consequence: cross-vendor agreement or a majority vote is evidence only; a deterministic oracle remains the completion authority.
   - Source: https://libraopen.library.virginia.edu/entities/publication/4ac33eeb-79b4-46e4-aef9-f6ec56a62286

4. **Hidden tests are a real agent-evaluation pattern.**
   - They can detect solutions that overfit visible tests, but must be protected from leakage and still require audit for coverage/validity.
   - Source: https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents

5. **Mutation testing is established but costly.**
   - It assesses whether tests detect injected meaningful defects; it is not a universal every-commit practice because of cost and equivalent-mutant issues.
   - Sources: https://onlinelibrary.wiley.com/doi/abs/10.1002/stvr.1675 and https://arxiv.org/abs/2103.08480

6. **Multi-agent critique/debate is not a truth machine.**
   - Useful as evidence gathering; results depend on model strength, diversity, objective feedback, and protocol design.
   - It cannot overrule a deterministic acceptance oracle or an owner decision.

## Inspect AI: possible role, not a V5 replacement

Inspect AI is a UK AI Security Institute / Meridian Labs framework for model and agent evaluation. It organizes a dataset, a solver/agent, tools/sandbox, and a scorer. It can run agents, external coding agents, retries, limits, logging, and held-out evaluation tasks.

For V5, Inspect AI could be an **external evaluation harness**:

```text
held-out task dataset
→ V5 runtime as solver/agent under test
→ V5 deterministic oracle as scorer
→ Inspect logs and aggregates results across models/configurations
```

It does not replace V5 seating, the V5 methodology engine, the broker, or the V5 runtime.

Official documentation: https://inspect.aisi.org.uk/

## Questions for the next Codex Chat research session

Ask these one at a time. Do not implement before the decision is recorded.

### A. Import and source-of-truth questions

1. Which exact SSC and V4 modules/behaviors must be ported into V5 for parity? Build a named inventory, not a vague “all capabilities” list.
2. Does “same named concepts” require importing the complete canonical M0–M33 procedure text, or a V5-owned executable contract/mapping for each ID?
3. Where do V4 and SSC disagree, and which behavior is intentional V5 replacement versus port-required behavior?
4. Is the canonical SSC methodology manual the semantic source of truth while V5 owns an executable representation, or must V5 fully own both text and implementation after porting?
5. What is the migration acceptance test for each ported capability: behavior parity, contract tests, hidden holdout, or a live workflow replay?

### B. Seating and retry questions

6. Confirm exact retry behavior at the **third** failure and the **twentieth** continuous failure:
   - retry the same model?
   - select next tiered model?
   - require a distinct vendor?
   - wait, quarantine, or escalate?
7. What resets the counters: a completed task, a passed deterministic verifier, a successful tool call, or a healthy real-task probe?
8. Is `MODEL_TIERS` a global prior only, or may a methodology explicitly select a lower-tier specialist lane (for example science, vision, long-context, or provider-side search)?
9. How is cross-vendor diversity enforced: vendor metadata registry, model-name parsing, or a catalog field from LiteLLM?

### C. Methodology-to-seat and broker questions

10. What are the canonical seat roles? List their exact names and intended purpose.
11. Does a methodology choose one role, a sequence of roles, or a panel? Specify this for each methodology that needs more than one seat.
12. What is the tool allowlist for every role/methodology combination?
13. Are broker grants based on the methodology ID, the role, risk level, workspace classification, or all of these?
14. Which SSC tools are ported directly: file read/write/edit/list, shell, grep/glob, web retrieval, image inspection, agent spawning, etc.? Which are deliberately excluded or replaced?
15. Can a role request a capability it was not granted, and if so should the broker refuse, escalate, or create a new owner question?

### D. Scratchpad, holdout, and closeout questions

16. What exact scratchpads exist: working, per-seat, task, closeout, reviewer, or others?
17. What is their storage format, visibility boundary, and deletion trigger?
18. Are sealed holdouts an explicit durability exception to the throwable-scratchpad rule?
19. Who can author a holdout, where is it stored, and who is prevented from reading it?
20. What may a closeout scratchpad contain before it is discarded: receipts, reasoning summary, tool log, redacted evidence, or only an operator note?

### E. M3/M4/M5/M5b/M20 execution questions

21. Which task/risk criteria activate the expensive M3/M4/M20 lane?
22. Should M3’s blind test-author and implementer be distinct roles/model vendors, distinct workspaces, or both?
23. Which invariants warrant targeted mutants, and what budget prevents mutation testing from making every task impractical?
24. Should V5 preserve M5 and M5b exactly as distinct mechanisms?
25. If V5 retains a checker-gated multi-candidate code build, what is its correct methodology name? It should not silently masquerade as canonical M5 or M5b.
26. Who owns an `UNVERIFIABLE` result: owner escalation, human verification queue, or incomplete task state?

### F. Evidence and evaluation questions

27. Which real V5 workflows will be the benchmark corpus for an A/B/C test of visible checks vs sealed holdout vs holdout plus targeted mutants?
28. Which metrics decide whether the full methodology earns its cost: hidden-defect detection, false reject rate, task cost, wall-clock time, flakiness, or recovery success?
29. Should Inspect AI be adopted only after V5 has stable deterministic tasks and scorers, or as part of building the new evaluation lane?

## Suggested first prompt for the next Codex Chat session

```text
Read docs/CORTEX-V5-METHODOLOGY-RESEARCH-HANDOFF-2026-08-15.md. Do not implement anything.

We are defining a standalone V5 that ports SSC/V4 behavior natively. First, build an
evidence-backed inventory of every SSC and V4 capability that must be considered for
porting into V5. Separate: (1) required parity behavior, (2) explicit V5 replacements,
(3) legacy behavior intentionally not desired, and (4) unknowns requiring owner decisions.

Use the canonical SSC methodology manual and the V4/V5 source. Report exact source files,
entry points, tests, and current V5 gaps. Do not assume that a methodology title means its
procedure is implemented.
```

## Key sources

- SSC canonical manual: `D:\claude\stupidly-simple-cortex\docs\methodology\WORK-METHODOLOGIES.md`
- SSC summon/runtime: `D:\claude\stupidly-simple-cortex\cortex_core\model_summon.py` and `agent_runtime.py`
- SSC temporal controller: `D:\claude\stupidly-simple-cortex\cortex_core\temporal_controller.py`
- V4 working checkout: `C:\Users\pujan\Documents\Codex\2026-08-12\cortex-v4-working`
- V5 seating research: `docs/MODEL-SEATING-RESEARCH-2026-08.md`
