import asyncio
from dataclasses import replace
from pathlib import Path

import cortex_v5.runtime as runtime_module
from cortex_v5.contracts import SinkResult, StreamCompletion, ToolCall
from cortex_v5.journal import Journal
from cortex_v5.runtime import CortexRuntime
from cortex_v5.settings import Settings
from cortex_v5.tools import ToolExecutor
from cortex_v5.verification import VerificationResult


class FakeRecorder:
    def __init__(self, journal):
        self.journal = journal

    def record(self, task_id, event_type, payload=None, **fields):
        self.journal.append_event(
            task_id, {"event_type": event_type, "payload": payload or {}, **fields}
        )
        return SinkResult(True, True, True)

    def close(self):
        pass


class FakeLiteLLM:
    def __init__(self, responses, models=("dynamic/code-high",)):
        self.responses = list(responses)
        self.models = models
        self.catalog_calls = 0
        self.chat_calls = []

    async def refresh_models(self):
        self.catalog_calls += 1
        return self.models

    async def chat_completion(self, **kwargs):
        self.chat_calls.append(kwargs)
        callback = kwargs.get("event_callback")
        if callback:
            await callback({"event": "litellm.sse", "has_content": True})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def aclose(self):
        pass


def make_runtime(tmp_path: Path, responses, *, sleeper=asyncio.sleep):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        allowed_root=tmp_path,
        litellm_url="http://litellm.invalid",
        litellm_api_key="secret",
        max_attempts=5,
        max_tool_rounds=4,
        default_max_tokens=128,
    )
    journal = Journal(settings.data_dir)
    lite = FakeLiteLLM(responses)
    runtime = CortexRuntime(
        settings,
        journal=journal,
        recorder=FakeRecorder(journal),
        litellm=lite,
        sleeper=sleeper,
    )
    return runtime, lite


async def no_wait(_seconds):
    return None


async def test_clear_task_runs_through_verification_and_completes(tmp_path):
    runtime, lite = make_runtime(
        tmp_path,
        [StreamCompletion(content="observable result", finish_reason="stop")],
    )
    submitted = await runtime.submit(
        {
            "prompt": "Research and report the observed state",
            "workspace": str(tmp_path),
            "acceptance": "A non-empty report is returned",
            "verification": {"require_output": True},
        }
    )
    result = await runtime.run(submitted["task_id"])
    assert result["status"] == "completed"
    assert result["verification_result"]["passed"] is True
    assert result["telemetry"]["gravebuster_ok"] is True
    assert lite.catalog_calls == 1
    assert lite.chat_calls[0]["tools"]
    assert lite.chat_calls[0]["stream"] is True
    assert lite.chat_calls[0]["max_tokens"] == 128


async def test_ambiguous_task_waits_for_human_then_becomes_ready(tmp_path):
    runtime, _ = make_runtime(tmp_path, [])
    submitted = await runtime.submit(
        {"prompt": "maybe deploy something", "workspace": str(tmp_path)}
    )
    assert submitted["status"] == "waiting_for_human"
    assert submitted["questions"]
    answered = await runtime.answer(
        submitted["task_id"],
        {"scope": "analysis of service A only", "acceptance": "return a report"},
    )
    assert answered["status"] == "ready"
    assert answered["questions"] == []


async def test_provider_failure_creates_new_attempt_before_retry(tmp_path):
    runtime, lite = make_runtime(
        tmp_path,
        [RuntimeError("provider failed"), StreamCompletion(content="recovered")],
        sleeper=no_wait,
    )
    submitted = await runtime.submit(
        {
            "prompt": "Research the local facts",
            "workspace": str(tmp_path),
            "acceptance": "non-empty answer",
        }
    )
    result = await runtime.run(submitted["task_id"])
    assert result["status"] == "completed"
    assert result["attempt_count"] == 2
    attempts = [
        receipt
        for receipt in runtime.journal.receipts(submitted["task_id"])
        if receipt.get("kind") == "model_attempt"
    ]
    assert len(attempts) == 2
    assert len({attempt["receipt_id"] for attempt in attempts}) == 2
    assert lite.catalog_calls == 2


async def test_catalog_failure_has_bounded_retry_and_cannot_be_hammered(tmp_path):
    now = [100.0]
    runtime, lite = make_runtime(tmp_path, [])
    runtime.clock = lambda: now[0]

    async def unavailable_catalog():
        lite.catalog_calls += 1
        raise RuntimeError("catalog down")

    lite.refresh_models = unavailable_catalog
    submitted = await runtime.submit(
        {
            "prompt": "Research the local facts",
            "workspace": str(tmp_path),
            "acceptance": "return a report",
        }
    )
    first = await runtime.run(submitted["task_id"])
    second = await runtime.run(submitted["task_id"])

    assert first["status"] == "waiting_for_model"
    assert first["waiting_reason"] == "live_catalog_unavailable"
    assert first["next_eligible_at"] == 130.0
    assert second["next_eligible_at"] == 130.0
    assert lite.catalog_calls == 1


async def test_attempt_limit_is_resumable_per_run_window(tmp_path):
    now = [100.0]
    runtime, _ = make_runtime(
        tmp_path,
        [RuntimeError("provider"), StreamCompletion(content="recovered")],
        sleeper=no_wait,
    )
    runtime.settings = replace(runtime.settings, max_attempts=1)
    runtime.clock = lambda: now[0]
    submitted = await runtime.submit(
        {
            "prompt": "Research the local facts",
            "workspace": str(tmp_path),
            "acceptance": "return a non-empty report",
        }
    )

    waiting = await runtime.run(submitted["task_id"])
    assert waiting["status"] == "waiting_for_model"
    assert waiting["waiting_reason"] == "attempt_window_exhausted"
    assert waiting["attempt_count"] == 1
    assert waiting["generation"] == 1
    assert (await runtime.run(submitted["task_id"]))["attempt_count"] == 1

    now[0] = waiting["next_eligible_at"] + 1
    completed = await runtime.run(submitted["task_id"])
    assert completed["status"] == "completed"
    assert completed["attempt_count"] == 2
    assert completed["generation"] == 2


async def test_real_tool_loop_writes_and_checks_workspace_artifact(tmp_path):
    (tmp_path / "checker.py").write_text(
        "from solution import answer\nassert answer() == 42\n", encoding="utf-8"
    )
    runtime, _ = make_runtime(
        tmp_path,
        [
            StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall(
                        "call-1",
                        "write",
                        {"path": "solution.py", "content": "def answer():\n    return 42\n"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            StreamCompletion(content="implemented and checked", finish_reason="stop"),
        ],
    )
    submitted = await runtime.submit(
        {
            "prompt": "Implement solution.py for the provided checker",
            "task_type": "build",
            "workspace": str(tmp_path),
            "acceptance": "python checker.py passes",
            "verification": {
                "commands": ["python checker.py"],
                "required_files": ["solution.py"],
                "protected_paths": ["checker.py"],
                "require_tool_call": True,
            },
        }
    )
    result = await runtime.run(submitted["task_id"])
    assert result["status"] == "completed"
    assert result["successful_tool_calls"] == 1
    assert (tmp_path / "solution.py").is_file()


async def test_verification_failure_counts_before_success_and_gets_receipt(tmp_path):
    (tmp_path / "checker.py").write_text(
        "from solution import answer\nassert answer() == 42\n", encoding="utf-8"
    )
    runtime, _ = make_runtime(
        tmp_path,
        [
            StreamCompletion(content="claimed complete", finish_reason="stop"),
            StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall(
                        "call-2",
                        "write",
                        {"path": "solution.py", "content": "def answer():\n    return 42\n"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            StreamCompletion(content="implemented", finish_reason="stop"),
        ],
        sleeper=no_wait,
    )
    submitted = await runtime.submit(
        {
            "prompt": "Implement solution.py for the protected checker",
            "task_type": "build",
            "workspace": str(tmp_path),
            "acceptance": "python checker.py passes",
            "verification": {
                "commands": ["python checker.py"],
                "required_files": ["solution.py"],
                "protected_paths": ["checker.py"],
            },
        }
    )

    result = await runtime.run(submitted["task_id"])

    assert result["status"] == "completed"
    assert result["attempt_count"] == 2
    state = runtime.journal.get_model_state("__cortex_v5_global_seating__", {})
    assert state["outcomes"]["dynamic/code-high"] == {"success": 1, "failure": 1}
    failures = [
        receipt
        for receipt in runtime.journal.receipts(submitted["task_id"])
        if receipt.get("kind") == "verification_failure"
    ]
    assert len(failures) == 1
    assert failures[0]["transition"]["action"] == "retry"


async def test_probe_failures_switch_in_ranked_order(tmp_path):
    runtime, lite = make_runtime(
        tmp_path,
        [
            RuntimeError("first"),
            RuntimeError("second"),
            RuntimeError("third"),
            StreamCompletion(content="recovered", finish_reason="stop"),
        ],
        sleeper=no_wait,
    )
    lite.models = ("a-secondary", "z-primary")
    submitted = await runtime.submit(
        {
            "prompt": "Research the local facts",
            "workspace": str(tmp_path),
            "acceptance": "return a non-empty report",
        }
    )

    result = await runtime.run(submitted["task_id"])

    assert result["status"] == "completed"
    attempts = [
        receipt
        for receipt in runtime.journal.receipts(submitted["task_id"])
        if receipt.get("kind") == "model_attempt"
    ]
    assert [attempt["model"] for attempt in attempts] == [
        "z-primary",
        "z-primary",
        "z-primary",
        "a-secondary",
    ]


async def test_model_hidden_run_is_rejected_after_gate_authorization(tmp_path, monkeypatch):
    class SpyToolExecutor(ToolExecutor):
        model_execute_calls = 0
        verification_execute_calls = 0

        def execute(self, name, args):
            type(self).model_execute_calls += 1
            return super().execute(name, args)

        def _execute_verification(self, command, allowed_commands):
            type(self).verification_execute_calls += 1
            return super()._execute_verification(command, allowed_commands)

    class TwoPassGate:
        calls = 0

        def __init__(self, verification_runner):
            self.runner = verification_runner

        def verify(self, *, task, **_kwargs):
            self.runner.authorize_verification(task["verification"]["commands"])
            type(self).calls += 1
            passed = type(self).calls == 2
            return VerificationResult(
                passed=passed,
                errors=() if passed else ("forced_first_verification_failure",),
            )

    monkeypatch.setattr(runtime_module, "ToolExecutor", SpyToolExecutor)
    monkeypatch.setattr(runtime_module, "VerificationGate", TwoPassGate)
    runtime, _ = make_runtime(
        tmp_path,
        [
            StreamCompletion(content="first attempt", finish_reason="stop"),
            StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall(
                        "hidden-1",
                        "run_command",
                        {"command": "python checker.py"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            StreamCompletion(content="second attempt", finish_reason="stop"),
        ],
        sleeper=no_wait,
    )
    submitted = await runtime.submit(
        {
            "prompt": "Implement the clearly specified local build",
            "task_type": "build",
            "workspace": str(tmp_path),
            "acceptance": "human checker succeeds",
            "verification": {"commands": ["python checker.py"]},
        }
    )

    result = await runtime.run(submitted["task_id"])

    assert result["status"] == "completed"
    assert SpyToolExecutor.model_execute_calls == 0
    assert SpyToolExecutor.verification_execute_calls == 0
    hidden_result = next(
        message for message in result["messages"] if message.get("tool_call_id") == "hidden-1"
    )
    assert "not present in the advertised schemas" in hidden_result["content"]
