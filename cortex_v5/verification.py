"""Deterministic completion gate; model self-claims are never verification."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

_EXECUTABLE_TASK_TYPES: Final[frozenset[str]] = frozenset(
    {"build", "debug", "migration", "evaluation", "amendment"}
)
_EVIDENCE_KINDS: Final[tuple[str, ...]] = (
    "wiring",
    "artifacts",
    "imports",
    "lint",
    "types",
    "e2e",
)
_EVIDENCE_ALIASES: Final[dict[str, str]] = {
    "artifact": "artifacts",
    "file": "artifacts",
    "files": "artifacts",
    "import": "imports",
    "type": "types",
    "typecheck": "types",
    "type_check": "types",
    "integration": "e2e",
    "end_to_end": "e2e",
    "wiring_artifacts": "wiring",
    "wiring/artifacts": "wiring",
}


@dataclass(frozen=True)
class VerificationResult:
    passed: bool
    checks: tuple[dict[str, Any], ...] = ()
    errors: tuple[str, ...] = ()
    command_outputs: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VerificationGate:
    """Evaluate explicit, mechanically observable acceptance conditions only."""

    def __init__(self, tool_executor: Any):
        self.tools = tool_executor

    def verify(
        self,
        *,
        task: Mapping[str, Any],
        output: str,
        methodology_ambiguous: bool,
        successful_tool_calls: int,
        telemetry: Mapping[str, Any] | Any | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> VerificationResult:
        """Return a fail-closed verdict from task data and executed checks.

        ``evidence`` is an optional trusted-caller channel for checks performed outside
        this gate.  Model output is intentionally never parsed as evidence.
        """

        spec = self._mapping(task.get("verification"))
        checks: list[dict[str, Any]] = []
        errors: list[str] = []
        command_outputs: list[dict[str, Any]] = []

        prompt = task.get("prompt")
        acceptance = task.get("acceptance")
        self._check(checks, errors, "non_ambiguous", methodology_ambiguous is False)
        self._check(
            checks,
            errors,
            "real_user_input",
            isinstance(prompt, str) and bool(prompt.strip()),
            source="task.prompt",
        )
        self._check(
            checks,
            errors,
            "acceptance_criterion",
            isinstance(acceptance, str) and bool(acceptance.strip()),
            source="task.acceptance",
        )
        self._check(
            checks,
            errors,
            "output_present",
            isinstance(output, str) and bool(output.strip()),
            source="model output presence only; not verification evidence",
        )

        if spec.get("real_input_id") is not None:
            real_input_id = spec.get("real_input_id")
            self._check(
                checks,
                errors,
                "real_input_id",
                isinstance(real_input_id, str) and bool(real_input_id.strip()),
                source="verification.real_input_id",
            )

        executable = bool(self._task_types(task) & _EXECUTABLE_TASK_TYPES)
        if executable or self._strict_true(spec.get("require_tool_call")):
            self._check(
                checks,
                errors,
                "tool_loop_used",
                isinstance(successful_tool_calls, int)
                and not isinstance(successful_tool_calls, bool)
                and successful_tool_calls > 0,
                source="runtime successful_tool_calls",
            )

        root = Path(getattr(self.tools, "root", task.get("workspace") or ".")).resolve()
        required_files = spec.get("required_files") or []
        if not isinstance(required_files, (list, tuple)):
            required_files = []
            self._check(checks, errors, "required_files_valid", False)
        file_results: list[bool] = []
        file_sources: list[str] = []
        for relative in required_files:
            label = str(relative)
            exists = self._required_file_exists(root, relative)
            file_results.append(exists)
            file_sources.append(label)
            self._check(
                checks,
                errors,
                f"required_file:{label}",
                exists,
                source=f"filesystem:{label}",
            )

        commands = spec.get("commands") or []
        if not isinstance(commands, (list, tuple)):
            commands = []
            self._check(checks, errors, "verification_commands_valid", False)
        normalized_commands = [
            command.strip() for command in commands if isinstance(command, str)
        ]
        commands_valid = len(normalized_commands) == len(commands) and all(normalized_commands)
        declared_commands = [command for command in normalized_commands if command]
        if executable:
            self._check(
                checks,
                errors,
                "verification_command_present",
                bool(declared_commands) and commands_valid,
                source="verification.commands",
            )
        elif commands and not commands_valid:
            self._check(checks, errors, "verification_commands_valid", False)

        runnable_commands = declared_commands if commands_valid else []
        authorization_ok = self._authorize_commands(runnable_commands)
        if runnable_commands and hasattr(self.tools, "authorize_verification"):
            self._check(
                checks,
                errors,
                "verification_commands_authorized",
                authorization_ok,
                source="human task verification.commands",
            )

        evidence_results: dict[str, list[tuple[bool, str]]] = {
            kind: [] for kind in _EVIDENCE_KINDS
        }
        if file_results:
            evidence_results["artifacts"].append(
                (all(file_results), "required files: " + ", ".join(file_sources))
            )

        command_results: list[bool] = []
        for command in runnable_commands:
            result = (
                self._execute_command(command)
                if authorization_ok
                else {"ok": False, "error": "verification command authorization failed"}
            )
            ok = self._command_succeeded(result)
            command_results.append(ok)
            output_text = self._command_output(result)
            command_outputs.append(
                {
                    "command": command,
                    "ok": ok,
                    "output": output_text[-4000:],
                }
            )
            self._check(
                checks,
                errors,
                f"command:{command}",
                ok,
                source="executed by ToolExecutor",
            )
            for kind in self._command_evidence_kinds(command):
                evidence_results[kind].append((ok, f"command:{command}"))

        if executable:
            self._check(
                checks,
                errors,
                "verification_commands_succeeded",
                bool(command_results) and all(command_results),
                source="executed command results",
            )

        for key, value in self._mapping(evidence).items():
            kind = self._evidence_kind(str(key))
            if kind:
                evidence_results[kind].append(
                    (self._caller_evidence_passed(value), f"caller evidence:{key}")
                )

        for kind in _EVIDENCE_KINDS:
            observations = evidence_results[kind]
            if observations:
                self._check(
                    checks,
                    errors,
                    kind,
                    all(passed for passed, _source in observations),
                    source=", ".join(source for _passed, source in observations),
                )

        # Change/executable work needs an observed artifact or a command that exercises
        # a real seam.  Merely returning prose and successfully running ``echo ok`` is
        # not completion evidence.
        if executable:
            wiring_or_artifacts = evidence_results["wiring"] + evidence_results["artifacts"]
            self._check(
                checks,
                errors,
                "wiring_or_artifacts",
                bool(wiring_or_artifacts)
                and any(passed for passed, _source in wiring_or_artifacts),
                source=(
                    ", ".join(source for _passed, source in wiring_or_artifacts)
                    or "no wiring/artifact evidence"
                ),
            )

        if self._strict_true(spec.get("require_external_telemetry")):
            status = self._mapping(telemetry)
            self._check(
                checks,
                errors,
                "telemetry_local",
                status.get("local_ok") is True,
                source="telemetry.local_ok",
            )
            self._check(
                checks,
                errors,
                "telemetry_gravebuster",
                status.get("gravebuster_ok") is True,
                source="telemetry.gravebuster_ok",
            )
            self._check(
                checks,
                errors,
                "telemetry_langfuse",
                status.get("langfuse_ok") is True,
                source="telemetry.langfuse_ok",
            )

        return VerificationResult(
            passed=not errors,
            checks=tuple(checks),
            errors=tuple(errors),
            command_outputs=tuple(command_outputs),
        )

    def _execute_command(self, command: str) -> dict[str, Any]:
        try:
            # ``run_command`` is the compatibility alias accepted by ToolExecutor;
            # model-facing schemas expose the canonical ``run`` name.
            raw = self.tools.execute("run_command", {"command": command})
        except Exception as exc:  # a verification runner failure is a failed check
            return {"ok": False, "error_type": type(exc).__name__}
        return self._mapping(raw)

    def _authorize_commands(self, commands: list[str]) -> bool:
        authorize = getattr(self.tools, "authorize_verification", None)
        if not callable(authorize):
            return True
        try:
            # These commands came from the human-submitted verification spec, not
            # from model output.  ToolExecutor still applies its runner sandbox.
            authorize(commands)
        except Exception:
            return False
        return True

    @classmethod
    def _command_succeeded(cls, result: Mapping[str, Any]) -> bool:
        if result.get("ok") is not True:
            return False
        payload = result.get("result")
        if isinstance(payload, Mapping) and "returncode" in payload:
            return payload.get("returncode") == 0
        if "returncode" in result:
            return result.get("returncode") == 0
        return True

    @classmethod
    def _command_output(cls, result: Mapping[str, Any]) -> str:
        if result.get("output") is not None:
            return str(result.get("output"))
        payload = result.get("result")
        if isinstance(payload, Mapping):
            stdout = str(payload.get("stdout") or "")
            stderr = str(payload.get("stderr") or "")
            return "\n".join(part for part in (stdout, stderr) if part)
        return str(result.get("error") or "")

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, Mapping):
            return dict(value)
        if hasattr(value, "to_dict"):
            converted = value.to_dict()
            return dict(converted) if isinstance(converted, Mapping) else {}
        if hasattr(value, "__dict__"):
            return dict(vars(value))
        return {}

    @staticmethod
    def _required_file_exists(root: Path, relative: Any) -> bool:
        if not isinstance(relative, str) or not relative.strip():
            return False
        try:
            candidate = (root / relative).resolve()
            candidate.relative_to(root)
            return candidate.is_file()
        except (OSError, RuntimeError, ValueError):
            return False

    @classmethod
    def _task_types(cls, task: Mapping[str, Any]) -> set[str]:
        methodology = cls._mapping(task.get("methodology"))
        values = (task.get("task_type"), methodology.get("task_type"))
        task_types: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                continue
            tokens = re.findall(r"[a-z]+", value.casefold())
            task_types.update("evaluation" if token == "eval" else token for token in tokens)
        return task_types

    @staticmethod
    def _command_evidence_kinds(command: str) -> set[str]:
        lowered = command.casefold()
        kinds: set[str] = set()
        patterns = {
            "wiring": r"\b(?:checker|e2e|integration|acceptance|smoke|chain)[\w.-]*\b",
            "imports": r"\b(?:import|py_compile|compileall|checker)[\w.-]*\b",
            "lint": r"\b(?:ruff|flake8|pylint|eslint|biome|clippy|golangci-lint)\b",
            "types": r"\b(?:mypy|pyright|pyre|tsc|typecheck|type-check)\b",
            "e2e": (
                r"\b(?:checker|e2e|integration|acceptance|smoke|pytest|unittest)[\w.-]*\b"
                r"|\b(?:go|cargo|npm|pnpm|yarn)\s+test\b"
            ),
        }
        for kind, pattern in patterns.items():
            if re.search(pattern, lowered):
                kinds.add(kind)
        return kinds

    @classmethod
    def _evidence_kind(cls, key: str) -> str | None:
        normalized = key.strip().casefold().replace("-", "_").replace(" ", "_")
        normalized = _EVIDENCE_ALIASES.get(normalized, normalized)
        return normalized if normalized in _EVIDENCE_KINDS else None

    @staticmethod
    def _caller_evidence_passed(value: Any) -> bool:
        if value is True:
            return True
        if not isinstance(value, Mapping):
            return False
        passed = value.get("passed") is True or value.get("ok") is True
        source = value.get("source") or value.get("command") or value.get("path")
        return passed and isinstance(source, str) and bool(source.strip())

    @staticmethod
    def _strict_true(value: Any) -> bool:
        return value is True

    @staticmethod
    def _check(
        checks: list[dict[str, Any]],
        errors: list[str],
        name: str,
        passed: bool,
        *,
        source: str | None = None,
    ) -> None:
        check: dict[str, Any] = {"name": name, "passed": bool(passed)}
        if source:
            check["source"] = source
        checks.append(check)
        if not passed:
            errors.append(name)
