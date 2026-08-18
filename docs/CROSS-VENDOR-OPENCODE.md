# Cross-vendor execution through OpenCode

**Status:** temporary operational path while the Cortex V5 coding-agent/runtime loop is not stable enough for long cross-vendor work. This is an execution runbook, not a new Cortex architecture layer.

## 1. Ownership split

Use the existing systems for the job they already own:

- **FOSSIL is the read brain.** Retrieve durable project/domain knowledge, provenance, lineage, historical failures, prior decisions, and research context there. Current live repository state still wins when freshness matters.
- **Cortex methodology is the routing brain.** `cortex_v5.methodology.MethodologyEngine` classifies task type, risk, ambiguity, and the required methodology IDs.
- **Cortex seating is the model-selection brain.** `cortex_v5.seating.SeatingManager` plus `MODEL_TIERS` ranks the live LiteLLM catalog. The first five research-grounded frontier seats are deliberately cross-vendor: xAI, OpenAI, Moonshot, Alibaba, and Google.
- **OpenCode is the temporary coding/research execution shell.** It receives an already-granulated task packet and runs the selected model against the authorized workspace.
- **LiteLLM/ckff is model transport.** It carries the selected seat; it does not redefine task meaning or Cortex lifecycle state.
- **Project tests, independent verification, holdouts, and CI remain the completion authority.** An OpenCode/model claim is never sufficient evidence by itself.

Do not make OpenCode session state authoritative Cortex state. Do not make FOSSIL active task state. Do not let LiteLLM fallback silently change a cross-vendor evaluation seat.

## 2. ckff route timeout contract

Operator-verified ckff dashboard text on 2026-08-18 records:

| Route | Use | Network timeout | Rule |
|---|---|---:|---|
| `https://ckffai.com/v1` | primary / Tencent node | 600 s | preferred for stability |
| `https://aws.ckffai.com/v1` | backup / AWS node | 180 s | non-stream requests exceeding 180 s can fail; prefer streaming or primary route |

Cortex V5 therefore uses a 600 second streaming HTTP read/inactivity window by default in `cortex_v5/litellm.py`. This is not permission to create ten-minute monolithic tasks. Provider/network ceilings still exist and reasoning/tool-call variance requires safety margin.

### Task-size rule

- Primary 600 s route: granulate model turns to an expected **300–450 s maximum**, preferably less.
- Backup 180 s route: granulate model turns to an expected **60–120 s maximum**.
- If a task times out on a shared route, **do not switch vendors and replay the same oversized packet**. Split the packet or move to the longer qualified route.
- Treat route/client timeout as infrastructure evidence. Do not automatically penalize model capability or seating quality for a shared transport ceiling.

## 3. OpenCode provider configuration

OpenCode supports custom OpenAI-compatible providers. Start from `opencode.example.json`, copy it to `opencode.json`, and point the `ckff` provider at the same LiteLLM-compatible endpoint used by Cortex.

The example uses the same persisted environment variable names as V5's `.env.example`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ckff": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ckff / Cortex cross-vendor seats",
      "options": {
        "baseURL": "{env:LITELLM_URL}",
        "apiKey": "{env:LITELLM_MASTER_KEY}"
      }
    }
  }
}
```

OpenCode substitutes process environment variables; it does not get V5's Python-side `.env` parsing for free. Export the local `.env` values into the process environment before invoking OpenCode, or use OpenCode's credential store. Keep credentials outside committed files.

The model passed to OpenCode is `ckff/<catalog-model-id>`, for example:

```bash
opencode run \
  --model ckff/grok-4.6 \
  --dir "$WORKSPACE" \
  --format json \
  --title "cortex-granule-W17-G3" \
  "$(cat task-packet.md)"
```

`opencode.example.json` deliberately asks before arbitrary shell commands and denies external-directory access, `git push`, `git commit`, `git reset --hard`, and `sudo`. A non-interactive run will not magically approve an `ask` permission. For a task that must run tests, either pre-allow the exact required test commands or use `--auto` **only inside an already-isolated/throwaway authorized workspace** where the explicit deny rules are adequate for the task. Do not weaken the permission boundary just to avoid an approval failure.

For repeated packets, a persistent OpenCode server may be used to avoid repeated startup/tool initialization:

```bash
opencode serve
opencode run --attach http://localhost:4096 --model ckff/grok-4.6 --dir "$WORKSPACE" "$(cat task-packet.md)"
```

Do not use `--continue` merely to make a task larger. Continue a session only when the next granule is explicitly bound to the same authorized work generation and the previous granule produced a valid checkpoint.

## 4. Seat selection procedure

Before invoking OpenCode:

1. Run `MethodologyEngine.decide(...)` on the requested work.
2. If the decision is ambiguous, stop for human resolution; OpenCode does not resolve authority ambiguity.
3. Refresh the live LiteLLM model catalog.
4. Rank candidates with `SeatingManager.rank(...)` using the decision's `task_type`, `risk`, and `routing_tags`.
5. Select the highest eligible seat from the research-grounded tier list.
6. When the task requires independent cross-vendor work, select additional seats from **different vendors** rather than same-vendor model variants.
7. Bind every invocation to the exact task/work generation and record the selected model and route as execution evidence.

The current top-five cross-vendor prior from `MODEL_TIERS` is:

1. `grok-4.6` — xAI
2. `gpt-5.6-sol` — OpenAI
3. `kimi-k3` — Moonshot
4. `qwen3.8-max` — Alibaba
5. `gemini-3.6-flash` — Google

Availability, methodology relevance, observed success/failure, and explicit route health still outrank blindly following a static list.

## 5. Granulated task packet

OpenCode should not receive an unbounded instruction such as “implement the whole issue.” Create one packet whose completion is independently observable.

Required fields:

```text
TASK ID / WORK UNIT / GENERATION
ROLE: worker | test-writer | reviewer | researcher | evaluator
MODEL SEAT: chosen Cortex seat
OBJECTIVE: one bounded outcome
AUTHORITATIVE INPUTS: exact requirement/version, current repo refs
FOSSIL CONTEXT: only relevant claims with provenance; current repo wins conflicts
AUTHORIZED SCOPE: exact workspace/files/tools/effects
NON-GOALS: what this packet must not expand into
ACCEPTANCE: deterministic check(s) for this granule
REQUIRED EVIDENCE: diff/test/research artifacts to return
TIME BUDGET: route-aware expected model-turn budget
STOP CONDITION: ambiguity, missing authority, failing prerequisite, or budget risk
HANDOFF: facts required by the next granule; no prose-only completion claim
```

A good granule usually changes or answers one thing and has one obvious verification boundary.

## 6. Which work goes through OpenCode while V5 is unstable?

Use OpenCode for work that benefits from a repository-aware frontier coding agent:

- implementation and refactoring;
- test writing;
- debugging and reproduction;
- code review/critique using a read-only/reviewer role;
- repository-scoped technical research;
- research synthesis when the OpenCode agent has an explicitly allowed search/citation tool surface;
- independent cross-vendor evaluator/critic runs.

Do **not** route these responsibilities through OpenCode merely because it is convenient:

- FOSSIL retrieval/provenance/lineage ownership;
- authoritative current requirement/project state;
- deterministic verification and test verdicts;
- mutation/holdout scoring machinery;
- CI/merge authority;
- Cortex lifecycle transitions.

The intended temporary flow is:

```text
live project + FOSSIL read brain
            |
            v
MethodologyEngine -> SeatingManager
            |
            v
pre-granulated work packet
            |
            v
OpenCode CLI -> selected ckff/LiteLLM model seat
            |
            v
workspace change / research artifact / critique
            |
            v
project tests + independent verification + CI
```

## 7. Timeout/failure classification

Record at least these classes separately:

- `model_output_failure` — model completed the request but produced incorrect/insufficient work;
- `provider_route_timeout` — ckff/upstream route exceeded its network/request envelope;
- `client_read_timeout` — local HTTP transport saw no stream data inside its read window;
- `stream_protocol_failure` — malformed/truncated SSE or invalid terminal semantics;
- `tool_execution_timeout` — a local tool/subprocess exceeded its own budget;
- `verification_timeout` — checker/test execution exceeded its budget.

Only `model_output_failure` should directly count as evidence of model capability failure. Shared transport failures should drive route health/backoff and packet granulation decisions instead.

## 8. References

- OpenCode provider configuration: https://opencode.ai/docs/providers
- OpenCode CLI `run` / `--model` / `--attach`: https://dev.opencode.ai/docs/cli/
- OpenCode config environment substitution: https://dev.opencode.ai/docs/config
- OpenCode permissions: https://opencode.ai/docs/permissions
- V5 seating evidence and tier provenance: `docs/MODEL-SEATING-RESEARCH-2026-08.md`
