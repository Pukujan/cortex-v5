# Cortex V5

Cortex V5 is an independent, local-first mechanical task runtime. The human is the only
authority. The orchestrator may execute explicit work, but it cannot widen scope, invent
acceptance criteria, or mark its own output complete.

V5 deliberately has no runtime dependency on Cortex V4 or `stupidly-simple-cortex` (SSC). Its
methodology catalog, routing policy, task state, 24-hour journal, receipts, telemetry, model
outcomes, and tool controls live in this repository. The one-time `.env` seed is local and
gitignored; after seeding, the running product reads only this repository's `.env`.

## Mechanical flow

1. Accept a task through the local HTTP API.
2. Deterministically classify its type and risk and select from the local M0–M33 methodology
   catalog.
3. If consequential scope remains ambiguous, persist decision-shaped questions and wait for
   the human.
4. Refresh LiteLLM's live `/v1/models` catalog and deterministically select an available seat.
5. Invoke that seat through streamed `/v1/chat/completions` with explicit tools, model,
   `stream: true`, and `max_tokens`.
6. Validate and execute only workspace-contained tools, returning each result through a new
   streamed chat request.
7. Run the explicit deterministic verification gate. A model saying “done” is not a gate.
8. Persist sanitized events and receipts. The first real acceptance additionally requires
   successful local, Gravebuster, and Langfuse observation.

For cross-vendor evaluation, `cortex-v5 multimodel` runs isolated candidate tool loops,
executes the same deterministic checker in each workspace, and records a checker-gated winner.
An optional adjudicator receives only sanitized candidate summaries; it cannot override a failed
checker. This is a separate evaluation path and does not change the reliability-first single-seat
selection policy.

Every retry or model switch is a distinct V5 attempt with a distinct receipt. The first real
task after five minutes of model inactivity acts as that model's probe; V5 never makes synthetic
health calls. Exhausted candidates leave the task waiting instead of creating a tight loop.

## Local setup

```powershell
python -m pip install -e ".[test]"
Copy-Item .env.example .env  # only when no one-time seed already exists
cortex-v5 serve --host 127.0.0.1 --port 8765
```

The API exposes task submission, status, human answers, and an explicit run trigger. By default
it binds to loopback. Set `CORTEX_V5_HTTP_BEARER` to require bearer authentication.

```powershell
$body = @{
  prompt = "Create solution.py for the supplied task"
  workspace = "C:\\work\\task"
  acceptance = "The supplied executable checker passes"
  verification = @{
    commands = @("python checker.py")
    required_files = @("solution.py")
    require_tool_call = $true
  }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8765/v1/tasks `
  -ContentType application/json -Body $body
```

## Verification

```powershell
python -m pytest
python -m ruff check .
```

The real first-acceptance command is documented by `cortex-v5 humaneval`; it submits one public
Hugging Face HumanEval item through the HTTP API and uses that item's executable tests as the
completion gate. It does not substitute a fixture or simulated model response.

## Cross-vendor arbitration

Run at least two live model IDs explicitly; model IDs are never inferred from provider names:

```powershell
cortex-v5 multimodel `
  --task-id HumanEval/0 `
  --models "grok-4.6,gpt-5.6-sol,gemini-3.1-pro-preview,kimi-k3" `
  --adjudicator-model "gpt-5.6-sol"
```

Each candidate receives a separate workspace and the same V5 file tools. The checker is run
inside the contained verification sandbox. The deterministic winner must pass the checker; an
adjudicator recommendation is recorded as telemetry only. The result uses the arbitration
methodology lane (`M5`, `M8`, `M19`, `M20`, `M29`, `M30`, `M31`).

No Railway deployment is part of this phase.
