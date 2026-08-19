# Cortex V5 — Model Seating Research & Design Notes (2026-08)

PLEASE NOTE LITELLM TIMEOUT IS 600 seconds for all models, please use give pre-granulated tasks otherwise they will fail to finish their task

**Status:** Research / design notes. Not a normative spec.
**Last updated:** 2026-08-14
**Scope:** Empirical grounding for the deterministic live-catalog seating policy,
the cross-vendor arbitration lane, the search/tool-surface gap, and the planned
stress-test harness.
**Labeling:** All benchmark figures are tagged as `measured` (V5's own BigCodeBench-Hard
run), `independent` (Artificial Analysis / NIST-CAISI / METR / LMArena / BenchLM /
Vals AI / Roboflow), or `vendor` (provider/model-card claims, not independently
reproduced at time of writing). Treat untagged numbers as claims.

---

## 1. Why this document exists

`PREFERENCE_HINTS` (a hard-coded model-priority list in `cortex_v5/seating.py`) was
removed on 2026-08-14 because it had **no provenance**: the entire cortex-v5
repository is a single commit (`31fde75`), and the list appeared at initial publish
with no rationale or design doc. The ordering it encoded was therefore unverifiable
policy dressed as fact.

Seating is now scored deterministically as:

```
score = (available, tag_overlap, success - failure, success, model_name)
```

The purpose of this document is to (a) record the measured evidence that should
drive any *future* ordering, (b) flag what is still unknown, and (c) give the
rationale for proposed tool-surface changes (search tool, per-lane tool allowlist).

---

## 2. Measured evidence: BigCodeBench-Hard on the Gravebuster pod

**Why BigCodeBench-Hard:** SWE-bench Verified is a gated dataset (401 without an HF
token/terms acceptance) and was judged too easy to be discriminating; HumanEval is
not a serious signal. BigCodeBench-Hard (`bigcode/bigcodebench-hard`, 148 tasks,
open) stresses multi-step, library-heavy code generation and is independently
scorable.

- **Pod:** `gravebuster` (Ubuntu 24.04, 16 cores, 30 GiB RAM, no GPU) over Tailscale.
- **Venv:** `/home/yoav/bcbvenv` — datasets 5.0.1 + 15 task-library deps.
- **Harness:** `/home/yoav/bcbharness/bcb_harness.py` — its own tool loop
  (`LiteLLMClient` + `ToolExecutor` read/write/edit/list, writes `solution.py`) and
  **direct verification** (`subprocess [sys.executable, checker.py]` inside the venv).
- **Subset:** 50 of 148 tasks (139 eligible after library allowlist).
- **Models:** qwen-3.6-max, grok-4.6, kimi-k3, gpt-5.6-sol, gemini-3.1-pro-preview
  (concurrency 4, checkpointed `results.json`).

### Results (measured, 2026-08-14)

| model | pass / total | pass % | infra-errors |
|---|---|---|---|
| grok-4.6 | 41 / 50 | **82.0** | 3 |
| gpt-5.6-sol | 34 / 48 | 70.8 | 6 |
| kimi-k3 | 25 / 48 | 52.1 | 2 |
| qwen-3.6-max | 25 / 50 | 50.0 | 9 |
| gemini-3.1-pro-preview | 6 / 48 | 12.5 | **38** |

**Interpretation notes:**
- gemini-3.1-pro-preview's 12.5% is **infra noise, not a quality signal** — 38 of 48
  attempts died on infrastructure errors in this harness. It needs a re-run with the
  infra issue diagnosed before any capability conclusion.
- gpt-5.6-sol and kimi-k3 recorded 48/50 (two tasks lost a per-model record, likely
  a crash/restart gap in the run).
- The V5 micro-sandbox (`sandbox_runner.py`) blocks site-packages reads,
  `os.putenv`, and env-clear, which breaks every third-party import. This run used
  **direct checker execution** in the venv, not the micro-sandbox, so pass rates
  reflect model quality + harness behavior, not sandbox artifacts.

---

## 3. Public benchmark consolidation (catalog-available models, 2026-08)

Primary independent aggregator used: **Artificial Analysis Intelligence Index v4.x**
(`independent`). Reliability legend: `indep` = independently verified; `vendor` =
provider/model-card claim. Full source list in §7.

Sorted by independent Intelligence Index (descending). Only models present in the
live LiteLLM catalog (37 entries → 34 unique chat/reasoning models + `grok-imagine-image`
[image-gen, excluded]).

| model | vendor | weights | AA-II | key coding/agentic | caveat |
|---|---|---|---|---|---|
| grok-4.6 | xAI | closed | 61 | GDPval 1753; CursorBench 69.9; Terminal-Bench 88.4 | measured 82.0% |
| gpt-5.5 | OpenAI | closed | 60 | SWE-V 80.6–82.6; Terminal-Bench 2.0 82.7; DeepSWE 70 | prev-gen frontier; weak knowledge/multilingual |
| gpt-5.6-sol | OpenAI | closed | ~60 | Coding Agent Index 80 (leads); DeepSWE 72.7; GPQA-D 94.6 | METR cheating flag; measured 70.8% |
| gpt-5.6-terra | OpenAI | closed | 57 | Coding Agent Index 77 | pricey for tier |
| kimi-k3 | Moonshot | open | 57 | FrontierSWE 81.2 (leads); Frontend Code #1; Terminal-Bench 88.3 | measured 52.1% |
| gemini-3.5-flash | Google | closed | 55/52 | Terminal-Bench 2.1 76.2; SWE-Pro 55.1 | 61% hallucination (indep) |
| gpt-5.6-luna | OpenAI | closed | 52 | Coding Agent Index 75 | best price/perf of 5.6 trio |
| gemini-3.6-flash | Google | closed | 52 | Terminal-Bench 2.1 78.0; SWE-Pro 58.7; OSWorld 83 | newest; ~2x faster/cheaper |
| gemini-3.5-flash-high | Google | closed | 52 | high-effort 3.5 flash | verbose |
| glm-5.2 | Zhipu | open | 51 | SWE-Pro 62.1; Terminal-Bench 2.1 81.0; NIST SWE-V 75.3 | top open-weights general |
| glm-5.2-metered | Zhipu | open | 51 | same model, metered API | = glm-5.2 |
| glm-5 | Zhipu | open | 50 | GDPval 1412; HLE 50.4 | first open ≥50; text-only 200K |
| minimax-m3 | MiniMax | open* | 45–55 | GDPval ~1670; GPQA 93 | *weights pending; multimodal |
| deepseek-v4-pro | DeepSeek | open | 44 | SWE-V ~74 (NIST) vs 80.6 (vendor) | CAISI ~8 mo behind frontier |
| mimo-v2.5-pro | Xiaomi | open | 43 | ~68 tok/s; text-only | (not MiniMax) |
| kimi-k2.7-code | Moonshot | open | ~42 | Kimi Code Bench v2 62.0 | coding specialist; vendor |
| kimi-k2-thinking | Moonshot | open | ~41 | SWE-V 71.3 (vendor); HLE 44.9 | verbose; vendor-reported |
| qwen3.6-plus | Alibaba | closed | 40 | SWE-V 78.8; Terminal-Bench 61.6 | slow (~56 tok/s) |
| deepseek-v4-flash | DeepSeek | open | 37/29 (max 52) | SWE-V 78.6–79; GPQA 88.1 | near-SOTA speed tier |
| glm-5-turbo | Zhipu | open | ~38 | 200K; text-only | thin data |
| minimax-m2.5 | MiniMax | open | 34 | GDPval 1215 | 88% hallucination; older |
| glm-4.7 | Zhipu | open | ~33.7 | SWE-V ~74; Terminal-Bench 41–64 | older, superseded |
| deepseek-v3.2 | DeepSeek | open | 32–33 | SWE-V ~70 | older; 128K ctx |
| gemini-3.1-pro-preview | Google | closed | ~41–48 | science #1 (GPQA-D 94.3%); LMArena ~1500 | measured 12.5% (38/48 infra); specialist |
| gemini-3.1-flash-lite-preview | Google | closed | 26 | GPQA 86.9; 363 tok/s | weak long-ctx (1M 12.3%), HLE 16 |
| gemini-3.1-pro-preview-search | Google | closed | — | 3.1-pro + search grounding | search variant |
| gemini-3.5-flash-search | Google | closed | — | 3.5-flash + search grounding | search variant |
| gemini-3.5-flash-search | Google | closed | — | (dup entry) | — |
| gemini-3.1-flash-lite-image | Google | closed | — | lite + image | image variant |
| qwen-3.6-max | Alibaba | open | no AA-II | — | measured 50.0%; superseded |
| qwen3.8-max | Alibaba | open | no AA-II | OSWorld-V 86.1; PaperBench 93; SWE-Pro 67.7 | vendor-claimed, unverified |
| qwen3.7-flash | Alibaba | closed | none | Roboflow vision 61.7 (#22/23) | budget vision; sparse |
| qwen3-coder-next | Alibaba | open | n/a | SWE-V 70.6–71.3; SWE-Pro 56.2; AIME 89 | coding specialist; 3B active |

**Data honesty notes:**
- `mimo-v2.5-pro` is **Xiaomi**, not MiniMax (catalog groups them loosely).
- No independent Intelligence Index exists for `qwen-3.6-max`, `qwen3.8-max`,
  `qwen3.7-flash`, `qwen3-coder-next`; their priors rest on measured + vendor claims.
- Anthropic Claude is **not in the V5 catalog** and is therefore excluded from
  seating despite leading several public leaderboards.
- grok-4.5 (not in table above): AA ranks 4th on the Intelligence Index; coding
  figures (DeepSWE 62, Terminal-Bench 2.1 83.3, SWE-Pro 64.7) are **vendor-claimed**;
  independent SWE-bench Verified 86.6 (Vals AI).

---

## 4. Proposed starter seating (based on §2 + §3)

Cross-vendor frontier set of **5 independent vendors** (OpenAI, xAI, Moonshot,
Alibaba, Google), consistent with the earlier rule that same-vendor variants and
open-weight clones don't count toward the independent floor of 3.

1. **grok-4.6** (xAI) — measured 82.0%, AA-II 61, cheap, reliable.
2. **gpt-5.6-sol** (OpenAI) — strongest public coding; measured 70.8%; demote if
   contamination/cheating risk is disqualifying (METR).
3. **kimi-k3** (Moonshot) — best open; measured 52.1%.
4. **qwen3.8-max** (Alibaba) — vendor-strong agentic; prefer over qwen-3.6-max.
5. **gemini-3.6-flash** (Google) — better agentic-coding and far more reliable in
   harness than gemini-3.1-pro-preview; reserve 3.1-pro as the science /
   long-context specialist lane.

Open reserves if >5 independent vendors wanted: **deepseek-v4-flash**,
**glm-5.2**. OpenAI fallback: **gpt-5.5** (AA-II 60) if sol is disqualified.

**Caveat:** this is a prior, not a conclusion. It should be revised as measured
evidence accumulates (including the A/B/C search experiment in §5).

### 4.1 Implemented tier list (2026-08-14)

This exact ordering is implemented as `MODEL_TIERS` in `cortex_v5/seating.py`
(lower index = higher priority). The score tuple is now
`(available, tag_overlap, -tier, success - failure, success, model)`:
availability and task-tag relevance outrank the tier, and the backoff/switch
thresholds override a persistently failing model. Catalog prefix duplicates
(`[aws]deepseek-v3.2`, `[grok] grok-4.6`) normalize to their unprefixed entry.

| # | model | vendor | basis |
|---|---|---|---|
| 1 | grok-4.6 | xAI | measured 82.0%; AA-II 61 |
| 2 | gpt-5.6-sol | OpenAI | Coding Agent Index 80; measured 70.8% |
| 3 | kimi-k3 | Moonshot | open #1; measured 52.1% |
| 4 | qwen3.8-max | Alibaba | vendor-strong agentic |
| 5 | gemini-3.6-flash | Google | AA-II 52; reliable workhorse |
| 6 | gpt-5.5 | OpenAI | AA-II 60 (prev-gen frontier) |
| 7 | gpt-5.6-terra | OpenAI | AA-II 57 |
| 8 | gemini-3.5-flash | Google | AA-II 55 |
| 9 | gemini-3.5-flash-high | Google | AA-II 52 |
| 10 | gpt-5.6-luna | OpenAI | AA-II 52 (budget) |
| 11 | glm-5.2 | Zhipu | AA-II 51; open general |
| 12 | glm-5.2-metered | Zhipu | = glm-5.2 (metered API) |
| 13 | glm-5 | Zhipu | AA-II 50 |
| 14 | minimax-m3 | MiniMax | AA-II 45-55; multimodal |
| 15 | deepseek-v4-pro | DeepSeek | AA-II 44 |
| 16 | deepseek-v4-flash | DeepSeek | open speed-tier, SWE-V 78.6 |
| 17 | mimo-v2.5-pro | Xiaomi | AA-II 43 |
| 18 | kimi-k2.7-code | Moonshot | coding specialist |
| 19 | kimi-k2-thinking | Moonshot | AA-II ~41 |
| 20 | gemini-3.1-pro-preview | Google | science #1; infra-heavy in harness |
| 21 | qwen3.6-plus | Alibaba | AA-II 40 |
| 22 | glm-5-turbo | Zhipu | AA-II ~38 |
| 23 | minimax-m2.5 | MiniMax | AA-II 34 |
| 24 | glm-4.7 | Zhipu | AA-II ~33.7 |
| 25 | deepseek-v3.2 | DeepSeek | AA-II 32-33 |
| 26 | gemini-3.1-flash-lite-preview | Google | AA-II 26 |
| 27 | qwen3-coder-next | Alibaba | SWE-V 70.6-71.3 specialist |
| 28 | grok-4.5 | xAI | prev-gen; vendor figures |
| 29 | qwen-3.6-max | Alibaba | measured 50.0%; superseded |
| 30 | gemini-3.1-pro-preview-search | Google | 3.1-pro + search grounding |
| 31 | gemini-3.5-flash-search | Google | 3.5-flash + search grounding |
| 32 | gemini-3.1-flash-lite-image | Google | lite + image variant |
| 33 | qwen3.7-flash | Alibaba | budget vision; sparse data |

Absent from the tuple (e.g. `grok-imagine-image`, image-gen only) share the lowest
priority tier, ordered deterministically by model name.

---

## 5. Tool-surface design notes (gap analysis)

### 5.1 Search models are not used as tools today
- The runtime tool set is fixed at `read`/`write`/`edit`/`list` (`tools.py`). No
  `search`/`web` tool exists, so no seated model can retrieve external content.
- The `-search` catalog variants (`gemini-3.1-pro-preview-search`,
  `gemini-3.5-flash-search`) receive **no routing boost** in `seating.rank()`:
  `overlap` matches literal name tokens against `{task_type, risk, *routing_tags}`,
  and `research` ≠ `search`, and routing tags are prefixed (`task:research`,
  `route:research`). They are selected only if explicitly passed or if they
  accumulate the best success history.
- Even when seated, a `-search` model grounds **provider-side**; V5 neither drives
  it nor sees/verifies what was retrieved. No citation provenance exists.

### 5.2 There is no tool broker
- One `ToolExecutor` is built per task invocation (`runtime.py`); every model, every
  round receives the same `tools.schemas()`.
- Methodology selection produces `methodology_ids` + `routing_tags`; these feed
  **seating overlap only**, never tool selection.
- **M29 "Seat access-control matrix (box model + forced-RAG per seat)"** exists in
  the catalog (`methodology.py`) as a **title only — zero implementation**. Tests
  assert M29 is *selected*, not that it does anything.
- What *is* enforced is file-path access control: `protected_paths`,
  `denied_paths`, the credential boundary (`.env`, `.ssh`, keys, etc.), and
  `_writable`/`_readable` checks. Tool *presence* is never differentiated.

### 5.3 Provenance & research today
- **Provenance (real):** local SQLite journal (`journal.py`) — per-attempt receipts
  (`receipt_id`, generation, model, probe flag), events keyed by `attempt_id`/round/
  timestamp, outcomes `{model, success, error_type}`, secret-sanitized telemetry
  export (Langfuse/OTel). No content-origin attribution or citation verification.
- **Research (declarative only):** `task_type "research"` selects M13/M14/M21/M22/
  M23/M25 — titles recorded into the decision; nothing enforces search, citations,
  or trust chains at runtime.

### 5.4 Proposed experiments
**A/B/C search test (10 tasks, methodology-gated):**
- **A** = `gemini-3.5-flash`, no search tool (baseline).
- **B** = `gemini-3.5-flash` + a real `search` tool (V5-driven retrieval returning
  content + citations; journaled → provenance). The tool itself calls the grounded
  model via the existing LiteLLM client.
- **C** = `gemini-3.5-flash-search` seated directly (provider-side grounding).
- **Gating:** grant `search` only when the decision's methodology set intersects
  `{M4, M9, M19, M20, M5}` (test-builder / critique / judge lanes).
- **Routing fidelity:** the current `bcb_harness.py` **bypasses the methodology
  loop** (direct `LiteLLMClient` + `ToolExecutor`). The experiment should run
  through the real runtime (HTTP API) so the gating and provenance are real.

**C/D shell test (parked):**
- C = 4 tools, no shell; D = + restricted `shell` tool (command allowlist, timeout,
  output cap, no network, credential boundary intact). Measure pass rate,
  verification-gate failures, and safety incidents.
- "Models summon opencode/aider" is a different architecture (recursive agent
  toolchains), not a flag flip; treated as a separate proposal.

---

## 6. Stress-test / fuzz harness plan (reproducible bugs → fixes)

Combines established patterns: **differential testing** (same payload across
models/seats; flag divergence), **LLM-generated fuzzing** (adversarial payloads),
**property testing** (invariants per run), and **dogfooding/red-team** (the system
attacks itself). Every crash persists the exact seeded payload and becomes a
regression test.

**Invariants the fuzzer must assert on every run:**
1. **Confinement** — no path escapes workspace; symlink loops rejected.
2. **Credential boundary** — `.env`, `.ssh`, keys never readable.
3. **Termination** — every task ends (RUNNING → COMPLETE/FAILED/WAITING), never hangs.
4. **Determinism** — same prompt → same methodology decision + seat ranking.
5. **Integrity** — receipt always persisted; journal never corrupt.
6. **Safety** — tool-round and token limits enforced; verification gate not bypassable.

**Targeted weak spots to fuzz first:** `ToolExecutor.execute` arg handling
(KeyError paths), methodology classifier edge tokens, seating state import,
journal concurrency, and the 10/20-failure switch thresholds.

---

## 7. Sources

### Measured (V5, 2026-08-14)
- BigCodeBench-Hard harness + results on `gravebuster` pod
  (`/home/yoav/bcbharness/`). Tasks: `bigcode/bigcodebench-hard`
  (<https://huggingface.co/datasets/bigcode/bigcodebench-hard>).

### Independent aggregators / evaluators
- Artificial Analysis — Intelligence Index v4.x, Coding Agent Index, model pages:
  <https://artificialanalysis.ai> (incl. 2026-08-11 gpt-5-6 and 2026-08-12 grok-4-6
  coverage).
- LMArena — Elo + coding sub-boards (incl. Frontend Code Arena, Kimi K3 #1):
  <https://lmarena.ai>; August 2026 snapshot via <https://swfte.com>.
- BenchLM — independent cross-model aggregation: <https://benchlm.ai>.
- NIST CAISI — independent DeepSeek V4 / GLM-5.2 evaluations.
- METR — GPT-5.6 Sol independent evaluation incl. high-cheating-rate finding:
  <https://metr.org>.
- Vals AI — grok-4.5 SWE-bench Verified 86.6: <https://vals.ai>.
- Roboflow — qwen3.7-flash vision evals.
- eesel — qwen3.7-flash notes.

### Primary / vendor (flagged as such in the text)
- OpenAI GPT-5.5 / 5.6 family launch pages and model cards.
- Google / DeepMind Gemini 3.1/3.5/3.6 model cards.
- xAI Grok 4.5/4.6 coverage (artificialanalysis.ai 2026-08-12; The New Stack).
- Alibaba Qwen3.8-Max release (VentureBeat, 2026-08-03).
- Moonshot Kimi K3 / K2.7-Code / K2-Thinking model cards + Kimi technical blog;
  independent review via apidog.
- DeepSeek V4 technical report: <https://arxiv.org/abs/2606.19348>.
- Z.ai GLM-5 / 5.1 / 5.2 materials.

### Repo references
- `cortex_v5/seating.py` — seating score + retry mechanics.
- `cortex_v5/arbitration.py` — multi-model arbitration lane (M5/M8/M19/M20/M29/M30/M31).
- `cortex_v5/tools.py` — `ToolExecutor` (4 tools; path/credential enforcement).
- `cortex_v5/runtime.py` — runtime state machine; methodology loop.
- `cortex_v5/methodology.py` — M0–M33 catalog; M29 is title-only (see §5.2).
- `bcb_harness.py` — pod benchmark harness (bypasses methodology loop; see §5.4).
