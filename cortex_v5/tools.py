"""Contained file and command tools for the Cortex V5 model loop."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .observability import sanitize


class ToolError(RuntimeError):
    pass


class _VerificationRunner:
    """Capability held by the verification gate, never exposed as a model tool."""

    def __init__(
        self,
        executor: ToolExecutor,
        commands: set[tuple[str, ...]],
    ) -> None:
        self.root = executor.root
        self._executor = executor
        self._allowed_commands = set(commands)

    def authorize_verification(self, commands: list[str]) -> None:
        self._allowed_commands = {tuple(self._executor._argv(command)) for command in commands}

    def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name != "run_command":
            return self._executor._tool_failure(f"unknown verification capability: {name}")
        try:
            result = self._executor._execute_verification(args["command"], self._allowed_commands)
            if result["returncode"] != 0:
                return {
                    "ok": False,
                    "result": sanitize(result),
                    "error": f"command exited with status {result['returncode']}",
                    "error_type": "CommandExitError",
                }
            return {"ok": True, "result": sanitize(result)}
        except (KeyError, OSError, ToolError, subprocess.SubprocessError) as exc:
            return self._executor._tool_failure(str(exc), type(exc).__name__)


class ToolExecutor:
    def __init__(
        self,
        root: str | Path,
        *,
        timeout: float = 15.0,
        max_output: int = 50_000,
        allowed_commands: list[list[str] | tuple[str, ...]] | None = None,
        protected_paths: list[str | Path] | None = None,
        denied_paths: list[str | Path] | None = None,
    ) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("workspace root must be a directory")
        self.timeout = timeout
        self.max_output = max_output
        self._initial_verification_commands = {
            tuple(map(str, command)) for command in (allowed_commands or [])
        }
        self.protected_paths = {
            self._path(str(path), allow_missing=True) for path in (protected_paths or [])
        }
        self.denied_paths = {
            self._path(str(path), allow_missing=True) for path in (denied_paths or [])
        }

    def verification_runner(self) -> _VerificationRunner:
        """Mint the non-model capability used only by ``VerificationGate``."""
        return _VerificationRunner(self, self._initial_verification_commands)

    @staticmethod
    def _argv(command: str) -> list[str]:
        return [item.strip('"') for item in shlex.split(command, posix=False)]

    @staticmethod
    def schemas() -> list[dict[str, Any]]:
        def schema(
            name: str, description: str, properties: dict[str, Any], required: list[str]
        ) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                        "additionalProperties": False,
                    },
                },
            }

        path = {"path": {"type": "string", "description": "Path relative to the workspace root"}}
        return [
            schema("read", "Read a workspace file", path, ["path"]),
            schema(
                "write",
                "Create or replace a workspace file",
                {**path, "content": {"type": "string"}},
                ["path", "content"],
            ),
            schema(
                "edit",
                "Replace one exact occurrence in a workspace file",
                {**path, "old": {"type": "string"}, "new": {"type": "string"}},
                ["path", "old", "new"],
            ),
            schema("list", "List files below a workspace directory", path, ["path"]),
        ]

    def _writable(self, path: Path) -> None:
        if self._sensitive(path):
            raise ToolError("path is denied by the credential boundary")
        if any(
            path == protected or protected in path.parents for protected in self.protected_paths
        ):
            raise ToolError("path is protected from model writes")

    def _readable(self, path: Path, *, allow_protected: bool = False) -> None:
        if self._sensitive(path):
            raise ToolError("path is denied by the credential boundary")
        if not allow_protected and self._protected(path):
            raise ToolError("path is a private verification control")

    def _protected(self, path: Path) -> bool:
        return any(
            path == protected or protected in path.parents for protected in self.protected_paths
        )

    def _sensitive(self, path: Path) -> bool:
        if any(path == denied or denied in path.parents for denied in self.denied_paths):
            return True
        try:
            relative = path.relative_to(self.root)
        except ValueError:
            return True
        secret_names = {
            ".aws",
            ".azure",
            ".codex",
            ".git",
            ".git-credentials",
            ".netrc",
            ".npmrc",
            ".pypirc",
            ".ssh",
            "credentials.json",
            "secrets.json",
            "id_ed25519",
            "id_rsa",
        }
        for part in relative.parts:
            lowered = part.casefold()
            if lowered == ".env" or lowered.startswith(".env."):
                return True
            if lowered in secret_names or lowered.endswith((".pem", ".key")):
                return True
        return False

    def _path(self, raw: str, *, allow_missing: bool = False) -> Path:
        candidate = self.root / raw
        try:
            resolved = candidate.resolve(strict=not allow_missing)
        except (FileNotFoundError, RuntimeError) as exc:
            raise ToolError("path does not exist or contains a symlink loop") from exc
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ToolError("path escapes workspace root") from exc
        if allow_missing:
            # Prevent a missing child from being placed below an escaping directory link.
            parent = candidate.parent.resolve(strict=True)
            try:
                parent.relative_to(self.root)
            except ValueError as exc:
                raise ToolError("symlink escapes workspace root") from exc
        return resolved

    def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == "read":
                path = self._path(str(args["path"]))
                self._readable(path)
                result: Any = path.read_text(encoding="utf-8")[: self.max_output]
            elif name == "write":
                path = self._path(str(args["path"]), allow_missing=True)
                self._writable(path)
                path.parent.mkdir(parents=False, exist_ok=True)
                content = str(args["content"])
                path.write_text(content, encoding="utf-8")
                result = {"bytes": len(content.encode("utf-8"))}
            elif name == "edit":
                path = self._path(str(args["path"]))
                self._writable(path)
                text = path.read_text(encoding="utf-8")
                old, new = str(args["old"]), str(args["new"])
                if not old or text.count(old) != 1:
                    raise ToolError("old text must occur exactly once")
                path.write_text(text.replace(old, new, 1), encoding="utf-8")
                result = {"changed": True}
            elif name == "list":
                path = self._path(str(args.get("path", ".")))
                self._readable(path)
                if not path.is_dir():
                    raise ToolError("path is not a directory")
                result = [
                    str(item.relative_to(self.root))
                    for item in sorted(path.iterdir())
                    if not self._sensitive(item.resolve(strict=False))
                    and not self._protected(item.resolve(strict=False))
                ][:1000]
            else:
                raise ToolError(f"tool is not advertised: {name}")
            return {"ok": True, "result": sanitize(result)}
        except (KeyError, OSError, ToolError, subprocess.SubprocessError) as exc:
            return self._tool_failure(str(exc), type(exc).__name__)

    @staticmethod
    def _tool_failure(message: str, error_type: str = "ToolError") -> dict[str, Any]:
        return {"ok": False, "error": sanitize(message), "error_type": error_type}

    def _execute_verification(
        self,
        command: str | list[str],
        allowed_commands: set[tuple[str, ...]],
    ) -> dict[str, Any]:
        argv = self._argv(command) if isinstance(command, str) else list(command)
        if tuple(argv) not in allowed_commands:
            raise ToolError("command was not exactly human-authorized")
        executable = Path(argv[0])
        if executable.is_absolute():
            try:
                executable.resolve(strict=True).relative_to(self.root)
            except ValueError as exc:
                raise ToolError("command executable is outside workspace") from exc
        family = executable.name.casefold()
        if family not in {"python", "python.exe"}:
            raise ToolError("only sandboxed workspace Python checkers are allowed")
        if "-c" in argv or "-m" in argv:
            raise ToolError("inline and module Python execution are refused")
        if len(argv) < 2:
            raise ToolError("Python checker path is required")
        checker = self._path(argv[1])
        self._readable(checker, allow_protected=True)
        if checker.suffix.casefold() != ".py":
            raise ToolError("Python checker must be a workspace .py file")
        argv = [
            sys.executable,
            "-I",
            str(Path(__file__).with_name("sandbox_runner.py")),
            str(self.root),
            str(checker),
            json.dumps(
                {
                    "denied": [str(path) for path in sorted(self.denied_paths)],
                    "protected": [str(path) for path in sorted(self.protected_paths)],
                }
            ),
            *argv[2:],
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=self.root,
                env={"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                shell=False,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise ToolError(f"command timed out after {self.timeout:g}s") from exc
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout[: self.max_output],
            "stderr": completed.stderr[: self.max_output],
            "truncated": len(completed.stdout) > self.max_output
            or len(completed.stderr) > self.max_output,
        }
