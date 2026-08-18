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

## 2. Timeout contract: provider, V5 client, and OpenCode are separate clocks

Operator-visible ckff dashboard text captured on 2026-08-18 records:

| Route | Use | Network timeout | Rule |
|---|---|---:|---|
| `https://ckffai.com/v1` | primary / Tencent node | 600 s | preferred for stability |
| `https://aws.ckffai.com/v1` | backup / AWS node | 180 s | non-stream requests exceeding 180 s can fail; prefer streaming or primary route |

Cortex V5's direct streaming client now uses a **600 second HTTP read/inactivity window** by default in `cortex_v5/litellm.py`. This replaces the historical 120 second default that could abort a quiet long-running stream before the preferred ckff route did.

OpenCode is a different clock. Current OpenCode configuration documents a provider-request `timeout` default of **300000 ms** plus a separate streamed `chunkTimeout`. `opencode.example.json` explicitly sets both to **600000 ms** for the preferred ckff route. However, an upstream OpenCode issue reports that some versions ignored a configured timeout above 300s and still failed at about 303s. Therefore the installed OpenCode version must be qualified before assuming one provider turn can actually use the full 600s.

### Task-size rule

- **Direct V5 streaming client on the 600 s route:** target individual model work well below the route ceiling, roughly **300–450 s maximum expected time**.
- **OpenCode on the 600 s route, until the installed version proves >300 s provider turns:** target each model turn at **<=240 s**. The OpenCode session may continue across multiple bounded turns/tool calls.
- **AWS 180 s backup route:** target each model turn at **60–120 s**.
- If a task times out on a shared route, **do not switch vendors and replay the same oversized packet**. Split the packet or move to the longer qualified route.
- Treat route/client/OpenCode timeout as infrastructure evidence. Do not automatically penalize model capability or seating quality for a shared transport ceiling.

Streaming prevents idle/non-stream failure modes; it does not make an absolute provider or client request ceiling disappear.

## 3. OpenCode provider configuration

OpenCode supports custom OpenAI-compatible providers. `opencode.example.json` defines one `ckff` provider with the current five qualified cross-vendor seats.

The example uses the same persisted environment variable names as V5's `.env.example` and pins the OpenCode request/chunk timeout to 600000 ms:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ckff": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "ckff / Cortex cross-vendor seats",
      "options": {
        "baseURL": "{env:LITELLM_URL}",
        "apiKey": "{env:LITELLM_MASTER_KEY}",
        "timeout": 600000,
        "chunkTimeout": 600000
      }
    }
  }
}
```

OpenCode substitutes process environment variables; it does not get V5's Python-side `.env` parsing for free. The dispatcher below loads V5's local settings and injects those two values into the OpenCode subprocess without putting credentials on the command line or in its receipt.

The checked-in OpenCode policy deliberately:

- denies external-directory access;
- disables recursive `task` subagent fan-out for this path;
- disables the `question` and doom-loop recovery tools so an already-granulated noninteractive packet stops instead of recursively widening itself;
- puts the wildcard bash `ask` rule **before** specific git/sudo denies because OpenCode uses last-match-wins permission ordering;
- makes the Plan agent explicitly read/web-only and denies arbitrary bash/edit access, so `--auto` cannot turn a reviewer/researcher/evaluator into a mutation seat.

For mutating Build work, `--auto` remains opt-in. Use it only with an already bounded/isolated workspace and the explicit deny rules in the OpenCode config.

Before trusting >300 s OpenCode turns on a machine, record the installed version and inspect the merged config:

```bash
opencode --version
opencode debug config
```

Then run a deliberate qualification probe if using the extra 300–600 s window materially matters. Until that probe passes, keep OpenCode provider turns under the conservative <=240 s target even though the ckff primary route itself allows 600 s.

## 4. Policy-driven dispatcher

Use the checked-in dispatcher rather than manually choosing a model when the task should follow V5 methodology/seating policy:

```bash
python -m cortex_v5.opencode_dispatch task-packet.md \
  --workspace "$WORKSPACE" \
  --acceptance "pytest -q" \
  --role worker \
  --dry-run
```

The dry run resolves V5 methodology, refreshes the live LiteLLM model catalog, applies `SeatingManager`, chooses the highest eligible qualified seat, and prints a credential-free execution plan. Remove `--dry-run` to execute it in the foreground:

```bash
python -m cortex_v5.opencode_dispatch task-packet.md \
  --workspace "$WORKSPACE" \
  --acceptance "pytest -q" \
  --role worker \
  --auto
```

For independent read-only cross-vendor critique/evaluation, request multiple seats:

```bash
python -m cortex_v5.opencode_dispatch review-packet.md \
  --workspace "$WORKSPACE" \
  --acceptance "produce an evidence-backed critique" \
  --role reviewer \
  --seats 3
```

The role mapping is deliberate:

- `worker` and `test-writer` -> OpenCode **Build** agent;
- `reviewer`, `researcher`, and `evaluator` -> OpenCode **Plan** agent.

The dispatcher refuses `--seats > 1` for mutating roles because multiple workers must not share one mutable workspace. Give each mutating seat its own isolated worktree/workspace instead.

The dispatcher runs OpenCode in the foreground with no outer Cortex wall-clock timeout. OpenCode may therefore perform multiple provider-bounded model turns and tool operations while each individual provider call remains bounded by the effective OpenCode/provider envelope. A successful OpenCode process is execution evidence, not Cortex completion.

For repeated manually managed packets, OpenCode may also be run with a persistent server to avoid repeated startup/tool initialization:

```bash
opencode serve
opencode run --attach http://localhost:4096 --model ckff/grok-4.6 --dir "$WORKSPACE" "$(cat task-packet.md)"
```

Do not use `--continue` merely to make a task larger. Continue a session only when the next granule is explicitly bound to the same authorized work generation and the previous granule produced a valid checkpoint.

## 5. Seat selection procedure

The dispatcher mechanically applies this sequence:

1. Run `MethodologyEngine.decide(...)` on the requested work.
2. If the decision is ambiguous, stop for human resolution; OpenCode does not resolve authority ambiguity.
3. Refresh the live LiteLLM model catalog.
4. Rank candidates with `SeatingManager.rank(...)` using the decision's `task_type`, `risk`, and `routing_tags`.
5. Select the highest eligible seat from the research-grounded tier list.
6. When the task requires independent cross-vendor work, select additional seats from **different vendors** rather than same-vendor model variants.
7. Bind the packet by SHA-256 and record the selected model, vendor, role/agent, methodology, route, workspace, and return code in a local dispatch receipt.

The current top-five cross-vendor prior from `MODEL_TIERS` is:

1. `grok-4.6` — xAI
2. `gpt-5.6-sol` — OpenAI
3. `kimi-k3` — Moonshot
4. `qwen3.8-max` — Alibaba
5. `gemini-3.6-flash` — Google

Availability, methodology relevance, observed success/failure, and explicit route health still outrank blindly following a static list.

## 6. Granulated task packet

OpenCode should not receive an unbounded instruction such as “implement the whole issue.” Create one packet whose completion is independently observable.

Required fields:

```text
TASK ID / WORK UNIT / GENERATION
ROLE: worker | test-writer | reviewer | researcher | evaluator
MODEL SEAT: chosen Cortex seat (filled by dispatcher when policy-selected)
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

## 7. Which work goes through OpenCode while V5 is unstable?

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

## 8. Timeout/failure classification

Record at least these classes separately:

- `model_output_failure` — model completed the request but produced incorrect/insufficient work;
- `provider_route_timeout` — ckff/upstream route exceeded its network/request envelope;
- `client_read_timeout` — local HTTP transport saw no stream data inside its read window;
- `opencode_request_timeout` — OpenCode aborted an individual provider turn before the upstream route completed;
- `stream_protocol_failure` — malformed/truncated SSE or invalid terminal semantics;
- `tool_execution_timeout` — a local tool/subprocess exceeded its own budget;
- `verification_timeout` — checker/test execution exceeded its budget.

Only `model_output_failure` should directly count as evidence of model capability failure. Shared transport failures should drive route health/backoff and packet granulation decisions instead.

## 9. References

- OpenCode provider configuration: https://opencode.ai/docs/providers
- OpenCode CLI `run` / `--model` / `--agent` / `--attach`: https://dev.opencode.ai/docs/cli/
- OpenCode configuration and provider timeout options: https://dev.opencode.ai/docs/config
- OpenCode agents: https://opencode.ai/docs/agents
- OpenCode permissions: https://opencode.ai/docs/permissions
- Known upstream OpenCode timeout-config issue: https://github.com/anomalyco/opencode/issues/30252
- V5 seating evidence and tier provenance: `docs/MODEL-SEATING-RESEARCH-2026-08.md`
