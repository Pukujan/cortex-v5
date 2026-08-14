from pathlib import Path

import pytest

from cortex_v5.contracts import SinkResult
from cortex_v5.verification import VerificationGate


class FakeTools:
    def __init__(
        self,
        root: Path,
        *,
        ok: bool = True,
        nested_returncode: int | None = None,
    ) -> None:
        self.root = root
        self.ok = ok
        self.nested_returncode = nested_returncode
        self.calls: list[tuple[str, dict]] = []
        self.authorized: list[str] | None = None

    def authorize_verification(self, commands):
        self.authorized = list(commands)

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        assert name == "run_command"
        if self.nested_returncode is not None:
            return {
                "ok": self.ok,
                "result": {
                    "returncode": self.nested_returncode,
                    "stdout": "checked",
                    "stderr": "",
                },
            }
        return {"ok": self.ok, "output": "checked"}


def build_task(**verification):
    return {
        "prompt": "Implement and wire solution.py for the supplied checker",
        "task_type": "build",
        "acceptance": "python checker.py exits successfully",
        "verification": verification,
    }


def test_verification_requires_observable_conditions(tmp_path: Path):
    (tmp_path / "solution.py").write_text("pass\n", encoding="utf-8")
    tools = FakeTools(tmp_path)
    result = VerificationGate(tools).verify(
        task=build_task(
            commands=["python checker.py"],
            required_files=["solution.py"],
            require_output=True,
            require_tool_call=True,
        ),
        output="done",
        methodology_ambiguous=False,
        successful_tool_calls=1,
    )
    assert result.passed
    assert all(check["passed"] for check in result.checks)
    assert tools.calls == [("run_command", {"command": "python checker.py"})]
    assert tools.authorized == ["python checker.py"]
    assert {"output_present", "wiring", "artifacts", "imports", "e2e"} <= {
        check["name"] for check in result.checks
    }


def test_prose_only_build_fails_closed(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task=build_task(require_output=True, require_tool_call=False),
        output="I implemented everything and all tests pass.",
        methodology_ambiguous=False,
        successful_tool_calls=0,
    )
    assert not result.passed
    assert {
        "tool_loop_used",
        "verification_command_present",
        "verification_commands_succeeded",
        "wiring_or_artifacts",
    } <= set(result.errors)


@pytest.mark.parametrize("task_type", ["debug", "migration", "evaluation", "amendment"])
def test_every_executable_change_type_requires_tools_and_commands(
    tmp_path: Path, task_type: str
):
    task = build_task()
    task["task_type"] = task_type
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task=task,
        output="finished",
        methodology_ambiguous=False,
        successful_tool_calls=0,
    )
    assert not result.passed
    assert {"tool_loop_used", "verification_command_present"} <= set(result.errors)


def test_unrelated_successful_command_is_not_wiring_or_artifact_evidence(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task=build_task(commands=["echo ok"]),
        output="done",
        methodology_ambiguous=False,
        successful_tool_calls=1,
    )
    assert not result.passed
    assert "command:echo ok" not in result.errors
    assert "wiring_or_artifacts" in result.errors


def test_inferred_methodology_type_cannot_bypass_executable_gate(tmp_path: Path):
    task = build_task(commands=["python checker.py"], required_files=[])
    task["task_type"] = None
    task["methodology"] = {"task_type": "build"}
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task=task,
        output="done",
        methodology_ambiguous=False,
        successful_tool_calls=0,
    )
    assert not result.passed
    assert "tool_loop_used" in result.errors


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("prompt", "   ", "real_user_input"),
        ("acceptance", "\t", "acceptance_criterion"),
    ],
)
def test_prompt_and_acceptance_are_always_nonblank(
    tmp_path: Path, field: str, value: str, error: str
):
    task = {
        "prompt": "Research the observed state",
        "acceptance": "Return a cited report",
        "task_type": "research",
        "verification": {},
    }
    task[field] = value
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task=task,
        output="report",
        methodology_ambiguous=False,
        successful_tool_calls=0,
    )
    assert not result.passed
    assert error in result.errors


def test_output_is_always_required_even_when_spec_disables_it(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task={
            "prompt": "Research the observed state",
            "acceptance": "Return a cited report",
            "task_type": "research",
            "verification": {"require_output": False},
        },
        output="  ",
        methodology_ambiguous=False,
        successful_tool_calls=0,
    )
    assert not result.passed
    assert "output_present" in result.errors


def test_non_executable_report_does_not_invent_tool_or_command_requirements(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task={
            "prompt": "Research and report the observed state",
            "task_type": "research",
            "acceptance": "Return a non-empty report",
            "verification": {},
        },
        output="observable result",
        methodology_ambiguous=False,
        successful_tool_calls=0,
    )
    assert result.passed
    names = {check["name"] for check in result.checks}
    assert "tool_loop_used" not in names
    assert "verification_command_present" not in names


def test_nested_nonzero_returncode_fails_even_when_outer_result_claims_ok(tmp_path: Path):
    (tmp_path / "solution.py").write_text("pass\n", encoding="utf-8")
    result = VerificationGate(FakeTools(tmp_path, nested_returncode=7)).verify(
        task=build_task(commands=["python checker.py"], required_files=["solution.py"]),
        output="done",
        methodology_ambiguous=False,
        successful_tool_calls=1,
    )
    assert not result.passed
    assert "command:python checker.py" in result.errors
    assert "verification_commands_succeeded" in result.errors
    assert result.command_outputs[0]["ok"] is False


def test_required_file_checks_remain_contained_to_workspace(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task=build_task(commands=["python checker.py"], required_files=["../outside.py"]),
        output="done",
        methodology_ambiguous=False,
        successful_tool_calls=1,
    )
    assert not result.passed
    assert "required_file:../outside.py" in result.errors
    assert "artifacts" in result.errors


def test_caller_evidence_is_reported_but_model_prose_is_not_read_as_evidence(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task=build_task(commands=["python -m ruff check ."]),
        output="The wiring and type checks passed, trust me.",
        methodology_ambiguous=False,
        successful_tool_calls=1,
        evidence={"wiring": {"passed": True, "source": "receipt:wiring-1"}, "types": False},
    )
    assert not result.passed
    checks = {check["name"]: check for check in result.checks}
    assert checks["wiring"]["passed"] is True
    assert checks["lint"]["passed"] is True
    assert checks["types"]["passed"] is False
    assert "imports" not in checks


def test_direct_telemetry_mapping_gates_all_three_sinks(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task={
            "prompt": "Research the observed state",
            "task_type": "research",
            "acceptance": "Return a report with all sink receipts",
            "verification": {"require_external_telemetry": True},
        },
        output="answer",
        methodology_ambiguous=False,
        successful_tool_calls=0,
        telemetry={"local_ok": True, "gravebuster_ok": False, "langfuse_ok": True},
    )
    assert not result.passed
    assert "telemetry_gravebuster" in result.errors
    assert "telemetry_local" not in result.errors
    assert "telemetry_langfuse" not in result.errors


def test_sink_result_object_is_also_accepted_for_telemetry(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task={
            "prompt": "Research the observed state",
            "task_type": "research",
            "acceptance": "Return a report with all sink receipts",
            "verification": {"require_external_telemetry": True},
        },
        output="answer",
        methodology_ambiguous=False,
        successful_tool_calls=0,
        telemetry=SinkResult(local_ok=True, gravebuster_ok=True, langfuse_ok=True),
    )
    assert result.passed


def test_verification_refuses_ambiguous_completion(tmp_path: Path):
    result = VerificationGate(FakeTools(tmp_path)).verify(
        task={
            "prompt": "Research the observed state",
            "task_type": "research",
            "acceptance": "Return a report",
            "verification": {},
        },
        output="model claims done",
        methodology_ambiguous=True,
        successful_tool_calls=0,
    )
    assert not result.passed
    assert "non_ambiguous" in result.errors
