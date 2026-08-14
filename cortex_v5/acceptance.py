"""Real public HumanEval acceptance submitted through the V5 loopback HTTP API."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from .api import create_app
from .journal import Journal
from .litellm import LiteLLMClient
from .observability import EventRecorder, sanitize
from .settings import Settings

HUMANEVAL_ROWS_URL = (
    "https://datasets-server.huggingface.co/first-rows"
    "?dataset=openai/openai_humaneval&config=openai_humaneval&split=test"
)


class AcceptancePreflightError(RuntimeError):
    """The real acceptance dependencies were not all observed healthy."""

    def __init__(self, phase: str, evidence: Mapping[str, Any]):
        super().__init__(f"acceptance preflight failed: {phase}")
        self.phase = phase
        self.evidence = sanitize(dict(evidence))


def _settings_checks(settings: Settings) -> dict[str, bool]:
    try:
        project_in_allowed_root = (
            settings.project_root.resolve().is_relative_to(settings.allowed_root.resolve())
        )
    except OSError:
        project_in_allowed_root = False
    return {
        "project_root": settings.project_root.is_dir(),
        "allowed_root": settings.allowed_root.is_dir(),
        "project_in_allowed_root": project_in_allowed_root,
        "litellm_url": bool(settings.litellm_url.strip()),
        "litellm_auth": bool(settings.litellm_api_key.strip()),
    }


def _telemetry_config_checks(env: Mapping[str, str]) -> dict[str, bool]:
    gravebuster = bool(
        str(env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip()
        or str(env.get("CORTEX_OTEL_COLLECTOR_HOST") or "").strip()
    )
    return {
        "gravebuster": gravebuster,
        "langfuse_host": bool(str(env.get("LANGFUSE_HOST") or "").strip()),
        "langfuse_public_auth": bool(str(env.get("LANGFUSE_PUBLIC_KEY") or "").strip()),
        "langfuse_private_auth": bool(str(env.get("LANGFUSE_SECRET_KEY") or "").strip()),
    }


async def preflight_acceptance(
    settings: Settings,
    *,
    journal_factory: Callable[..., Journal] = Journal,
    recorder_factory: Callable[..., EventRecorder] = EventRecorder,
    litellm_factory: Callable[..., LiteLLMClient] = LiteLLMClient,
) -> dict[str, Any]:
    """Observe every external acceptance dependency without issuing a chat call."""

    settings_checks = _settings_checks(settings)
    evidence: dict[str, Any] = {
        "config": settings_checks,
        "models": {"live": False, "count": 0, "catalog_sha256": None},
        "telemetry": {
            "local_ok": False,
            "gravebuster_ok": False,
            "langfuse_ok": False,
        },
    }
    if not all(settings_checks.values()):
        raise AcceptancePreflightError("required_v5_config", evidence)

    journal: Journal | None = None
    recorder: EventRecorder | None = None
    litellm: LiteLLMClient | None = None
    try:
        journal = journal_factory(settings.data_dir)
        recorder = recorder_factory(journal, repo_root=settings.project_root)
        telemetry_config = _telemetry_config_checks(recorder.env)
        evidence["config"].update(telemetry_config)
        if not all(telemetry_config.values()):
            raise AcceptancePreflightError("required_telemetry_config", evidence)

        litellm = litellm_factory(
            settings.litellm_url,
            api_key=settings.litellm_api_key,
        )
        try:
            models = await litellm.refresh_models()
        except Exception as exc:
            evidence["models"]["error_type"] = type(exc).__name__
            raise AcceptancePreflightError("live_model_catalog", evidence) from None
        if not models:
            raise AcceptancePreflightError("nonempty_model_catalog", evidence)
        evidence["models"] = {
            "live": True,
            "count": len(models),
            "catalog_sha256": hashlib.sha256("\n".join(models).encode()).hexdigest(),
        }

        preflight_id = f"acceptance-preflight-{uuid.uuid4().hex}"
        try:
            observed = recorder.record(
                preflight_id,
                "acceptance.preflight",
                {"model_count": len(models), "purpose": "humaneval_acceptance"},
            )
        except Exception as exc:
            evidence["telemetry"]["error_type"] = type(exc).__name__
            raise AcceptancePreflightError("telemetry_observation", evidence) from None
        evidence["telemetry"] = {
            "local_ok": observed.local_ok,
            "gravebuster_ok": observed.gravebuster_ok,
            "langfuse_ok": observed.langfuse_ok,
            "event_type": "acceptance.preflight",
        }
        if not observed.acceptance_ready:
            raise AcceptancePreflightError("telemetry_observation", evidence)
        return sanitize(evidence)
    finally:
        if litellm is not None:
            await litellm.aclose()
        if recorder is not None:
            recorder.close()
        if journal is not None:
            journal.close()


async def fetch_humaneval_task(task_id: str | None = None) -> dict[str, Any]:
    """Fetch an actual row from Hugging Face; fixtures are intentionally unsupported."""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(HUMANEVAL_ROWS_URL)
        response.raise_for_status()
        payload = response.json()
    rows = [dict(item.get("row") or {}) for item in payload.get("rows") or []]
    if not rows:
        raise RuntimeError("Hugging Face returned no HumanEval rows")
    if task_id:
        match = next((row for row in rows if row.get("task_id") == task_id), None)
        if match is None:
            raise RuntimeError(
                f"HumanEval task {task_id!r} was not in the live first-rows response"
            )
        return match
    return rows[0]


def _prepare_workspace(settings: Settings, row: dict[str, Any]) -> Path:
    parent = settings.project_root / "acceptance-workspace"
    parent.mkdir(parents=True, exist_ok=True)
    safe_id = str(row["task_id"]).replace("/", "-").replace("\\", "-")
    workspace = parent / f"{safe_id}-{uuid.uuid4().hex[:8]}"
    workspace.mkdir()
    checker = (
        "import importlib.util\n"
        "from pathlib import Path\n\n"
        "path = Path(__file__).with_name('solution.py')\n"
        "spec = importlib.util.spec_from_file_location('solution', path)\n"
        "if spec is None or spec.loader is None:\n"
        "    raise RuntimeError('solution.py could not be loaded')\n"
        "solution = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(solution)\n\n"
        f"{row['test']}\n\n"
        f"check(getattr(solution, {str(row['entry_point'])!r}))\n"
    )
    (workspace / "checker.py").write_text(checker, encoding="utf-8")
    (workspace / "source.json").write_text(
        json.dumps(
            {
                "dataset": "openai/openai_humaneval",
                "source": HUMANEVAL_ROWS_URL,
                "task_id": row["task_id"],
                "entry_point": row["entry_point"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return workspace


def _acceptance_request(row: Mapping[str, Any], workspace: Path) -> dict[str, Any]:
    return {
        "prompt": (
            f"Solve the real public Hugging Face HumanEval task {row['task_id']}. "
            "Use the write tool to create a complete Python module named solution.py in the "
            "workspace. Preserve the required function signature and implement the behavior "
            "using only the public problem statement below as task authority. The mechanical "
            "verification gate will privately test the finished module after readiness.\n\n"
            f"{row['prompt']}"
        ),
        "task_type": "build",
        "risk": "medium",
        "workspace": str(workspace),
        "acceptance": "python checker.py exits successfully against the public dataset test",
        "metadata": {
            "dataset": "openai/openai_humaneval",
            "task_id": row["task_id"],
            "source_url": HUMANEVAL_ROWS_URL,
        },
        "verification": {
            "commands": ["python checker.py"],
            "required_files": ["solution.py"],
            "protected_paths": ["checker.py", "source.json"],
            "require_output": True,
            "require_tool_call": True,
            "require_external_telemetry": True,
            "real_input_id": row["task_id"],
        },
        "autostart": True,
    }


def _wait_state(task: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        task.get("status"),
        task.get("generation"),
        task.get("attempt_count"),
        task.get("next_eligible_at"),
        task.get("waiting_reason"),
        task.get("model"),
    )


async def _poll_task(
    client: httpx.AsyncClient,
    task_id: str,
    submitted: Mapping[str, Any],
    *,
    timeout_seconds: float,
    poll_interval: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> dict[str, Any]:
    deadline = monotonic() + timeout_seconds
    latest = dict(submitted)
    triggered_wait_state: tuple[Any, ...] | None = None
    terminal = {"completed", "failed", "waiting_for_human"}
    while monotonic() < deadline:
        await sleeper(poll_interval)
        status_response = await client.get(f"/v1/tasks/{task_id}")
        status_response.raise_for_status()
        latest = dict(status_response.json())
        status = latest.get("status")
        if status in terminal:
            break
        if status != "waiting_for_model":
            triggered_wait_state = None
            continue
        next_eligible = float(latest.get("next_eligible_at") or 0)
        wait_state = _wait_state(latest)
        if next_eligible <= wall_clock() and wait_state != triggered_wait_state:
            run_response = await client.post(f"/v1/tasks/{task_id}/run", json={})
            run_response.raise_for_status()
            triggered_wait_state = wait_state
    return latest


async def run_humaneval_acceptance(
    settings: Settings,
    *,
    human_eval_task_id: str | None = None,
    timeout_seconds: float = 1_800,
) -> dict[str, Any]:
    preflight = await preflight_acceptance(settings)
    row = await fetch_humaneval_task(human_eval_task_id)
    workspace = _prepare_workspace(settings, row)
    app = create_app(settings=settings)

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve(sockets=[listener]))

    try:
        for _ in range(200):
            if server.started:
                break
            if server_task.done():
                raise RuntimeError("V5 acceptance HTTP server failed to start")
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("V5 acceptance HTTP server start timed out")

        headers = (
            {"Authorization": f"Bearer {settings.http_bearer}"} if settings.http_bearer else {}
        )
        base_url = f"http://127.0.0.1:{port}"
        request = _acceptance_request(row, workspace)
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30) as client:
            response = await client.post("/v1/tasks", json=request)
            response.raise_for_status()
            submitted = response.json()
            task_id = str(submitted["task_id"])
            latest = await _poll_task(
                client,
                task_id,
                submitted,
                timeout_seconds=timeout_seconds,
            )

        return {
            "acceptance": "public_hugging_face_humaneval",
            "dataset_task_id": row["task_id"],
            "task_id": latest["task_id"],
            "status": latest["status"],
            "workspace": str(workspace),
            "attempt_count": latest.get("attempt_count"),
            "model": latest.get("model"),
            "verification_result": latest.get("verification_result"),
            "telemetry": latest.get("telemetry"),
            "preflight": preflight,
        }
    finally:
        server.should_exit = True
        await asyncio.gather(server_task, return_exceptions=True)
        listener.close()
