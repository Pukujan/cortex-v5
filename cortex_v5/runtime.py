"""Mechanical Cortex V5 state machine joining methodology, routing, tools, and gates."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from .contracts import ModelChoice, SinkResult, TaskStatus
from .journal import Journal
from .litellm import LiteLLMClient
from .methodology import MethodologyEngine
from .observability import EventRecorder, sanitize
from .seating import SeatingManager
from .settings import Settings
from .tools import ToolExecutor
from .verification import VerificationGate

_GLOBAL_SEATING_STATE = "__cortex_v5_global_seating__"
_RETRY_DELAY_SECONDS = 30.0
_SYSTEM_PROMPT = """You are an execution seat inside Cortex V5. The human task below is the
only authority. Do not widen its scope, invent requirements, or claim completion without the
mechanical verification gate. Work only inside the supplied workspace using the provided tools.
Inspect before editing, make the smallest complete change, run relevant checks, and never expose
credentials. Use tool calls whenever the task requires a file or command; a prose code block is
not a substitute for writing the requested artifact. When the work is ready for verification,
return a concise factual summary.
"""


class RuntimeErrorState(RuntimeError):
    """A fail-closed state transition error suitable for an HTTP conflict response."""


class CortexRuntime:
    """Independent V5 controller. No method reads or imports any legacy Cortex runtime."""

    def __init__(
        self,
        settings: Settings,
        *,
        journal: Journal | None = None,
        recorder: EventRecorder | None = None,
        methodology: MethodologyEngine | None = None,
        litellm: LiteLLMClient | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings
        self.journal = journal or Journal(settings.data_dir)
        self.recorder = recorder or EventRecorder(self.journal, repo_root=settings.project_root)
        self.methodology = methodology or MethodologyEngine()
        self.litellm = litellm or LiteLLMClient(
            settings.litellm_url,
            api_key=settings.litellm_api_key,
            event_callback=None,
        )
        self.clock = clock
        self.sleeper = sleeper
        self._locks: dict[str, asyncio.Lock] = {}

    async def close(self) -> None:
        await self.litellm.aclose()
        self.recorder.close()
        self.journal.close()

    def _workspace(self, requested: str | None) -> Path:
        root = Path(requested or self.settings.allowed_root).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise RuntimeErrorState("workspace must be an existing directory")
        try:
            root.relative_to(self.settings.allowed_root)
        except ValueError as exc:
            raise RuntimeErrorState("workspace escapes CORTEX_V5_ALLOWED_ROOT") from exc
        return root

    def get_task(self, task_id: str) -> dict[str, Any]:
        task = self.journal.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return dict(task)

    def task_snapshot(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        task["event_count"] = len(self.journal.events(task_id))
        task["receipt_count"] = len(self.journal.receipts(task_id))
        return task

    async def record_unexpected_failure(self, task_id: str, exc: Exception) -> None:
        """Make an escaped background exception visible without persisting its raw text."""
        try:
            task = self.get_task(task_id)
        except KeyError:
            return
        task["status"] = str(TaskStatus.FAILED)
        task["failure_reason"] = "unexpected_runtime_error"
        task["last_error"] = type(exc).__name__
        task["updated_at"] = self.clock()
        self.journal.put(task_id, task)
        await self._record(
            task_id,
            "task.failed",
            {"reason": "unexpected_runtime_error", "error_type": type(exc).__name__},
        )

    async def submit(self, values: Mapping[str, Any]) -> dict[str, Any]:
        prompt = str(values.get("prompt") or "").strip()
        if not prompt:
            raise ValueError("prompt must not be blank")
        workspace = self._workspace(values.get("workspace"))
        acceptance = values.get("acceptance")
        decision = self.methodology.decide(
            prompt,
            task_type=values.get("task_type"),
            risk=values.get("risk"),
            workspace=str(workspace),
            acceptance=str(acceptance) if acceptance else None,
        )
        task_id = str(uuid.uuid4())
        now = self.clock()
        status = TaskStatus.WAITING_FOR_HUMAN if decision.ambiguous else TaskStatus.READY
        task: dict[str, Any] = {
            "task_id": task_id,
            "prompt": prompt,
            "task_type": values.get("task_type"),
            "risk": values.get("risk"),
            "workspace": str(workspace),
            "acceptance": acceptance,
            "max_tokens": int(values.get("max_tokens") or self.settings.default_max_tokens),
            "metadata": sanitize(dict(values.get("metadata") or {})),
            "verification": dict(values.get("verification") or {}),
            "status": str(status),
            "methodology": decision.to_dict(),
            "questions": list(decision.questions),
            "answers": {},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "attempt_count": 0,
            "generation": 0,
            "successful_tool_calls": 0,
            "output": "",
            "verification_result": None,
            "telemetry": {
                "local_ok": False,
                "gravebuster_ok": False,
                "langfuse_ok": False,
            },
            "created_at": now,
            "updated_at": now,
        }
        self.journal.put(task_id, task)
        self.journal.append_receipt(
            task_id,
            {
                "receipt_id": str(uuid.uuid4()),
                "kind": "task_submission",
                "task_id": task_id,
                "status": str(status),
                "created_at": now,
            },
        )
        await self._record(
            task_id,
            "methodology.decision",
            {
                "task_type": decision.task_type,
                "risk": decision.risk,
                "methodology_ids": decision.methodology_ids,
                "ambiguous": decision.ambiguous,
                "question_count": len(decision.questions),
            },
        )
        return self.task_snapshot(task_id)

    async def answer(self, task_id: str, answers: Mapping[str, Any]) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != TaskStatus.WAITING_FOR_HUMAN:
            raise RuntimeErrorState("task is not waiting for human answers")
        merged = {**dict(task.get("answers") or {}), **dict(answers)}
        workspace = str(merged.get("workspace") or task["workspace"])
        acceptance = merged.get("acceptance") or task.get("acceptance")
        decision = self.methodology.decide(
            task["prompt"],
            task_type=task.get("task_type"),
            risk=task.get("risk"),
            answers=merged,
            workspace=workspace,
            acceptance=str(acceptance) if acceptance else None,
        )
        task.update(
            {
                "answers": sanitize(merged),
                "workspace": str(self._workspace(workspace)),
                "acceptance": acceptance,
                "methodology": decision.to_dict(),
                "questions": list(decision.questions),
                "status": str(
                    TaskStatus.WAITING_FOR_HUMAN if decision.ambiguous else TaskStatus.READY
                ),
                "updated_at": self.clock(),
            }
        )
        if merged:
            answer_text = json.dumps(sanitize(merged), ensure_ascii=False, sort_keys=True)
            task["messages"].append(
                {"role": "user", "content": f"Human clarification: {answer_text}"}
            )
        self.journal.put(task_id, task)
        await self._record(
            task_id,
            "human.answer",
            {
                "answer_keys": sorted(merged),
                "ambiguity_resolved": not decision.ambiguous,
                "remaining_questions": len(decision.questions),
            },
        )
        return self.task_snapshot(task_id)

    async def _record(
        self, task_id: str, event_type: str, payload: Mapping[str, Any] | None = None
    ) -> SinkResult:
        result = await asyncio.to_thread(
            self.recorder.record, task_id, event_type, sanitize(dict(payload or {}))
        )
        task = self.journal.get(task_id)
        if task is not None:
            aggregate = dict(task.get("telemetry") or {})
            aggregate["local_ok"] = bool(aggregate.get("local_ok")) or result.local_ok
            aggregate["gravebuster_ok"] = (
                bool(aggregate.get("gravebuster_ok")) or result.gravebuster_ok
            )
            aggregate["langfuse_ok"] = bool(aggregate.get("langfuse_ok")) or result.langfuse_ok
            aggregate["last"] = result.to_dict()
            task["telemetry"] = aggregate
            task["updated_at"] = self.clock()
            self.journal.put(task_id, task)
        return result

    def _save_seating(self, seats: SeatingManager) -> None:
        self.journal.set_model_state(_GLOBAL_SEATING_STATE, seats.export_state())

    def _append_attempt_receipt(
        self,
        task_id: str,
        *,
        attempt_id: str,
        generation: int,
        model: str,
        probe: bool,
        kind: str = "model_attempt",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.journal.append_receipt(
            task_id,
            sanitize(
                {
                    "receipt_id": str(uuid.uuid4()),
                    "kind": kind,
                    "attempt_id": attempt_id,
                    "generation": generation,
                    "model": model,
                    "real_task_probe": probe,
                    "created_at": self.clock(),
                    **dict(detail or {}),
                }
            ),
        )

    async def run(self, task_id: str) -> dict[str, Any]:
        lock = self._locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            task = self.get_task(task_id)
            if task["status"] == TaskStatus.COMPLETED:
                return self.task_snapshot(task_id)
            if task["status"] == TaskStatus.WAITING_FOR_HUMAN:
                raise RuntimeErrorState("human answers are required before execution")
            next_eligible_at = float(task.get("next_eligible_at") or 0)
            if task["status"] == TaskStatus.WAITING_FOR_MODEL and next_eligible_at > self.clock():
                return self.task_snapshot(task_id)
            if not self.settings.litellm_url or not self.settings.litellm_api_key:
                raise RuntimeErrorState("V5-local LiteLLM configuration is incomplete")

            verification_spec = dict(task.get("verification") or {})
            workspace = Path(task["workspace"]).resolve(strict=True)
            denied_paths: list[Path] = []
            data_dir = self.settings.data_dir.resolve()
            try:
                data_dir.relative_to(workspace)
            except ValueError:
                pass
            else:
                denied_paths.append(data_dir)
            tool_executor = ToolExecutor(
                workspace,
                protected_paths=list(verification_spec.get("protected_paths") or []),
                denied_paths=denied_paths,
            )
            verification = VerificationGate(tool_executor.verification_runner())
            seats = SeatingManager(state=self.journal.get_model_state(_GLOBAL_SEATING_STATE, {}))
            forced_model: str | None = None
            invocation_start_attempts = int(task.get("attempt_count", 0))

            while (
                int(task.get("attempt_count", 0)) - invocation_start_attempts
                < self.settings.max_attempts
            ):
                task = self.get_task(task_id)
                task["status"] = str(TaskStatus.RUNNING)
                task.pop("waiting_reason", None)
                task.pop("next_eligible_at", None)
                task["updated_at"] = self.clock()
                self.journal.put(task_id, task)

                try:
                    catalog = await self.litellm.refresh_models()
                except Exception as exc:  # no model call occurred; retain the task for later
                    task["status"] = str(TaskStatus.WAITING_FOR_MODEL)
                    task["waiting_reason"] = "live_catalog_unavailable"
                    task["next_eligible_at"] = self.clock() + _RETRY_DELAY_SECONDS
                    task["last_error"] = type(exc).__name__
                    task["updated_at"] = self.clock()
                    self.journal.put(task_id, task)
                    await self._record(
                        task_id,
                        "model.catalog_failure",
                        {
                            "error_type": type(exc).__name__,
                            "next_eligible_at": task["next_eligible_at"],
                        },
                    )
                    return self.task_snapshot(task_id)

                await self._record(
                    task_id,
                    "model.catalog_refreshed",
                    {"available_model_count": len(catalog)},
                )
                now = self.clock()
                decision = dict(task["methodology"])
                ranked = seats.rank(
                    catalog,
                    task_type=str(decision["task_type"]),
                    risk=str(decision["risk"]),
                    methodology_tags=tuple(decision.get("routing_tags") or ()),
                    now=now,
                )
                ranked_models = tuple(item.model for item in ranked)
                choice = self._choose_model(ranked, forced_model, now)
                forced_model = None
                if choice is None:
                    eligible_times = [item.eligible_at for item in ranked]
                    task["status"] = str(TaskStatus.WAITING_FOR_MODEL)
                    task["waiting_reason"] = "candidates_exhausted"
                    task["next_eligible_at"] = min(eligible_times, default=now + 300)
                    task["updated_at"] = now
                    self.journal.put(task_id, task)
                    await self._record(
                        task_id,
                        "model.candidates_exhausted",
                        {"next_eligible_at": task["next_eligible_at"]},
                    )
                    return self.task_snapshot(task_id)

                task["attempt_count"] = int(task.get("attempt_count", 0)) + 1
                task["generation"] = int(task.get("generation", 0)) + 1
                task["model"] = choice.model
                task["status"] = str(TaskStatus.RUNNING)
                task["updated_at"] = now
                self.journal.put(task_id, task)
                attempt_id = str(uuid.uuid4())
                self._append_attempt_receipt(
                    task_id,
                    attempt_id=attempt_id,
                    generation=task["generation"],
                    model=choice.model,
                    probe=choice.is_real_task_probe,
                )
                await self._record(
                    task_id,
                    "model.attempt_started",
                    {
                        "attempt_id": attempt_id,
                        "generation": task["generation"],
                        "model": choice.model,
                        "real_task_probe": choice.is_real_task_probe,
                    },
                )

                try:
                    completion = await self._tool_loop(
                        task_id,
                        task,
                        choice,
                        attempt_id,
                        tool_executor,
                    )
                except Exception as exc:
                    transition = seats.record_result(
                        choice.model,
                        success=False,
                        now=self.clock(),
                        was_probe=choice.is_real_task_probe,
                        candidates=ranked_models,
                    )
                    self._save_seating(seats)
                    self.journal.append_outcome(
                        task_id,
                        {
                            "attempt_id": attempt_id,
                            "model": choice.model,
                            "success": False,
                            "error_type": type(exc).__name__,
                            "transition": transition.to_dict(),
                        },
                    )
                    self._append_attempt_receipt(
                        task_id,
                        attempt_id=attempt_id,
                        generation=task["generation"],
                        model=choice.model,
                        probe=choice.is_real_task_probe,
                        kind="model_failure",
                        detail={
                            "error_type": type(exc).__name__,
                            "transition": transition.to_dict(),
                        },
                    )
                    await self._record(
                        task_id,
                        "model.attempt_failed",
                        {
                            "attempt_id": attempt_id,
                            "model": choice.model,
                            "error_type": type(exc).__name__,
                            "transition": transition.to_dict(),
                        },
                    )
                    task = self.get_task(task_id)
                    if transition.action == "wait":
                        task["status"] = str(TaskStatus.WAITING_FOR_MODEL)
                        task["waiting_reason"] = transition.reason
                        task["next_eligible_at"] = transition.eligible_at
                        self.journal.put(task_id, task)
                        return self.task_snapshot(task_id)
                    forced_model = transition.model
                    delay = max(1.0, transition.eligible_at - self.clock())
                    task["status"] = str(TaskStatus.WAITING_FOR_MODEL)
                    task["next_eligible_at"] = self.clock() + delay
                    task["waiting_reason"] = transition.reason
                    self.journal.put(task_id, task)
                    await self.sleeper(delay)
                    continue

                task = self.get_task(task_id)
                task["status"] = str(TaskStatus.VERIFYING)
                task["output"] = completion.content
                task["updated_at"] = self.clock()
                self.journal.put(task_id, task)
                await self._record(
                    task_id,
                    "verification.started",
                    {"attempt_id": attempt_id, "model": choice.model},
                )
                task = self.get_task(task_id)
                result = await asyncio.to_thread(
                    verification.verify,
                    task=task,
                    output=completion.content,
                    methodology_ambiguous=bool(task["methodology"].get("ambiguous")),
                    successful_tool_calls=int(task.get("successful_tool_calls", 0)),
                    telemetry=task.get("telemetry"),
                )
                task["verification_result"] = result.to_dict()
                task["updated_at"] = self.clock()
                self.journal.put(task_id, task)
                transition = seats.record_result(
                    choice.model,
                    success=result.passed,
                    now=self.clock(),
                    was_probe=choice.is_real_task_probe,
                    candidates=ranked_models,
                )
                self._save_seating(seats)
                await self._record(
                    task_id,
                    "verification.result",
                    {
                        "attempt_id": attempt_id,
                        "passed": result.passed,
                        "checks": result.checks,
                        "errors": result.errors,
                        "transition": transition.to_dict(),
                    },
                )

                if result.passed:
                    task = self.get_task(task_id)
                    task["status"] = str(TaskStatus.COMPLETED)
                    task["completed_at"] = self.clock()
                    task["updated_at"] = self.clock()
                    self.journal.put(task_id, task)
                    self.journal.append_outcome(
                        task_id,
                        {
                            "attempt_id": attempt_id,
                            "model": choice.model,
                            "success": True,
                            "verification": "passed",
                            "transition": transition.to_dict(),
                        },
                    )
                    await self._record(
                        task_id,
                        "task.completed",
                        {"attempt_id": attempt_id, "model": choice.model},
                    )
                    return self.task_snapshot(task_id)

                self.journal.append_outcome(
                    task_id,
                    {
                        "attempt_id": attempt_id,
                        "model": choice.model,
                        "success": False,
                        "verification": "failed",
                        "errors": result.errors,
                        "transition": transition.to_dict(),
                    },
                )
                self._append_attempt_receipt(
                    task_id,
                    attempt_id=attempt_id,
                    generation=task["generation"],
                    model=choice.model,
                    probe=choice.is_real_task_probe,
                    kind="verification_failure",
                    detail={
                        "errors": result.errors,
                        "transition": transition.to_dict(),
                    },
                )
                feedback = ", ".join(result.errors) or "unspecified verification failure"
                task = self.get_task(task_id)
                task["messages"].append(
                    {
                        "role": "user",
                        "content": (
                            "Mechanical verification failed: "
                            f"{feedback}. Inspect the actual workspace and repair only "
                            "these failures."
                        ),
                    }
                )
                revised = self.methodology.decide(
                    task["prompt"],
                    task_type=task.get("task_type"),
                    risk=task.get("risk"),
                    answers=task.get("answers"),
                    workspace=task.get("workspace"),
                    acceptance=task.get("acceptance"),
                )
                task["methodology"] = revised.to_dict()
                if revised.ambiguous:
                    task["questions"] = list(revised.questions)
                    task["status"] = str(TaskStatus.WAITING_FOR_HUMAN)
                    self.journal.put(task_id, task)
                    return self.task_snapshot(task_id)
                if transition.action == "wait":
                    task["status"] = str(TaskStatus.WAITING_FOR_MODEL)
                    task["waiting_reason"] = transition.reason
                    task["next_eligible_at"] = transition.eligible_at
                    self.journal.put(task_id, task)
                    return self.task_snapshot(task_id)
                forced_model = transition.model
                delay = max(1.0, transition.eligible_at - self.clock())
                task["status"] = str(TaskStatus.WAITING_FOR_MODEL)
                task["waiting_reason"] = transition.reason
                task["next_eligible_at"] = self.clock() + delay
                self.journal.put(task_id, task)
                await self.sleeper(delay)
                continue

            task = self.get_task(task_id)
            task["status"] = str(TaskStatus.WAITING_FOR_MODEL)
            task["waiting_reason"] = "attempt_window_exhausted"
            task["next_eligible_at"] = self.clock() + _RETRY_DELAY_SECONDS
            task["updated_at"] = self.clock()
            self.journal.put(task_id, task)
            await self._record(
                task_id,
                "model.attempt_window_exhausted",
                {
                    "attempts_this_run": self.settings.max_attempts,
                    "attempt_count": task["attempt_count"],
                    "next_eligible_at": task["next_eligible_at"],
                },
            )
            return self.task_snapshot(task_id)

    @staticmethod
    def _choose_model(
        ranked: tuple[ModelChoice, ...], forced_model: str | None, now: float
    ) -> ModelChoice | None:
        if forced_model:
            forced = next(
                (
                    choice
                    for choice in ranked
                    if choice.model == forced_model and choice.eligible_at <= now
                ),
                None,
            )
            if forced:
                return forced
        return next((choice for choice in ranked if choice.eligible_at <= now), None)

    async def _tool_loop(
        self,
        task_id: str,
        task: dict[str, Any],
        choice: ModelChoice,
        attempt_id: str,
        tools: ToolExecutor,
    ) -> Any:
        messages = list(task["messages"])
        advertised_names = {
            str(schema.get("function", {}).get("name")) for schema in tools.schemas()
        }
        for round_index in range(self.settings.max_tool_rounds + 1):
            await self._record(
                task_id,
                "sse.started",
                {
                    "attempt_id": attempt_id,
                    "round": round_index,
                    "model": choice.model,
                },
            )

            async def sse_event(event: dict[str, Any], round_number: int = round_index) -> None:
                self.journal.append_event(
                    task_id,
                    {
                        **sanitize(event),
                        "attempt_id": attempt_id,
                        "round": round_number,
                        "timestamp": self.clock(),
                    },
                )

            completion = await self.litellm.chat_completion(
                model=choice.model,
                messages=messages,
                tools=tools.schemas(),
                stream=True,
                max_tokens=int(task["max_tokens"]),
                event_callback=sse_event,
                temperature=0,
            )
            await self._record(
                task_id,
                "sse.completed",
                {
                    "attempt_id": attempt_id,
                    "round": round_index,
                    "model": choice.model,
                    "termination": completion.termination,
                    "finish_reason": completion.finish_reason,
                    "event_count": completion.event_count,
                    "tool_call_count": len(completion.tool_calls),
                },
            )
            if not completion.tool_calls:
                task = self.get_task(task_id)
                task["messages"] = messages + [{"role": "assistant", "content": completion.content}]
                self.journal.put(task_id, task)
                return completion
            if round_index >= self.settings.max_tool_rounds:
                raise RuntimeErrorState("tool round limit exhausted")

            assistant_calls = []
            for call in completion.tool_calls:
                assistant_calls.append(
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": json.dumps(call.arguments, ensure_ascii=False),
                        },
                    }
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": completion.content or None,
                    "tool_calls": assistant_calls,
                }
            )
            for call in completion.tool_calls:
                await self._record(
                    task_id,
                    "tool.call",
                    {
                        "attempt_id": attempt_id,
                        "round": round_index,
                        "call_id": call.call_id,
                        "tool": call.name,
                    },
                )
                if call.name not in advertised_names:
                    result = {
                        "ok": False,
                        "error": "tool name is not present in the advertised schemas",
                        "error_type": "ToolError",
                    }
                else:
                    result = await asyncio.to_thread(tools.execute, call.name, call.arguments)
                if result.get("ok"):
                    task["successful_tool_calls"] = int(task.get("successful_tool_calls", 0)) + 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(sanitize(result), ensure_ascii=False),
                    }
                )
                await self._record(
                    task_id,
                    "tool.result",
                    {
                        "attempt_id": attempt_id,
                        "round": round_index,
                        "call_id": call.call_id,
                        "tool": call.name,
                        "ok": bool(result.get("ok")),
                        "error_type": result.get("error_type"),
                    },
                )
                current = self.get_task(task_id)
                current["messages"] = messages
                current["successful_tool_calls"] = task["successful_tool_calls"]
                self.journal.put(task_id, current)
        raise RuntimeErrorState("tool loop terminated unexpectedly")
