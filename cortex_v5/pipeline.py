"""Named-caller methodology pipeline. IDs are rungs, not titles.

Ported from SSC ``summon_agent`` / ``arbitrate`` and V4 mechanical chains without
importing SSC or reading its corpus. Multi-route work uses isolated workspaces.
The checker, not model agreement, decides a winner. A third seat is used only
on disagreement (DAFE). Closeout prose cannot complete a rung.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .observability import sanitize
from .seating import SeatingManager
from .tools import ToolExecutor

MULTI_ROUTE_TYPES = frozenset({"arbitration", "debug"})
MULTI_ROUTE_METHODS = frozenset({"M5", "M28", "M32"})
_SYSTEM = (
    "You are one isolated seat in a Cortex V5 mechanical pipeline. "
    "The human task is the only authority. Work only in this workspace. "
    "Use tools to write artifacts. Do not claim completion; the checker decides."
)


@dataclass
class PipelineRung:
    name: str
    ok: bool
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PipelineResult:
    ok: bool
    mode: str
    rungs: tuple[PipelineRung, ...]
    models: tuple[str, ...]
    winner: str | None
    winner_workspace: str | None
    winner_tool_calls: int = 0
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["rungs"] = [rung.to_dict() for rung in self.rungs]
        return sanitize(payload)


def requires_multi_route(task: Mapping[str, Any]) -> bool:
    methodology = dict(task.get("methodology") or {})
    task_type = str(methodology.get("task_type") or task.get("task_type") or "")
    ids = set(methodology.get("methodology_ids") or ())
    explicit = [str(item).strip() for item in (task.get("models") or ()) if str(item).strip()]
    if len(explicit) >= 2:
        return True
    if any(part in MULTI_ROUTE_TYPES for part in task_type.replace("+", " ").split()):
        return True
    return bool(ids & MULTI_ROUTE_METHODS)


def observe_workspace(workspace: Path) -> dict[str, Any]:
    """M32: inventory before any hypothesis or edit."""

    files = sorted(
        str(path.relative_to(workspace)).replace("\\", "/")
        for path in workspace.rglob("*")
        if path.is_file()
    )
    return {
        "workspace": str(workspace),
        "file_count": len(files),
        "files": files[:80],
        "hypothesis_not_yet_formed": True,
        "observation_first": True,
    }


async def default_preflight(fossil: Any, prompt: str) -> dict[str, Any]:
    """M1: ask FOSSIL if configured; otherwise record an explicit none-exists."""

    if fossil is None or not getattr(fossil, "configured", lambda: False)():
        return {
            "ok": True,
            "grounded": False,
            "none_exists": True,
            "reason": "fossil_not_configured",
        }
    result = fossil.search(prompt[:500])
    grounded = bool(result.get("ok") and result.get("hits"))
    return {
        "ok": True,
        "grounded": grounded,
        "none_exists": not grounded,
        "fossil": {
            "ok": result.get("ok"),
            "pending": result.get("pending"),
            "reason": result.get("reason"),
        },
    }


def select_panel(
    seats: SeatingManager,
    catalog: Sequence[str],
    *,
    task: Mapping[str, Any],
    now: float,
    size: int = 2,
) -> tuple[str, ...]:
    explicit = tuple(
        dict.fromkeys(str(item).strip() for item in (task.get("models") or ()) if str(item).strip())
    )
    if len(explicit) >= 2:
        available = set(catalog)
        return tuple(model for model in explicit if model in available)[: max(2, size)]
    methodology = dict(task.get("methodology") or {})
    ranked = seats.rank(
        catalog,
        task_type=str(methodology.get("task_type") or "generic"),
        risk=str(methodology.get("risk") or "low"),
        methodology_tags=tuple(methodology.get("routing_tags") or ()),
        now=now,
    )
    eligible = [choice.model for choice in ranked if choice.eligible_at <= now]
    return tuple(eligible[: max(2, size)])


class MechanicalPipeline:
    """Origin-to-frontier caller: observe → preflight → seat → fan-out → check."""

    def __init__(
        self,
        *,
        litellm: Any,
        fossil: Any,
        clock: Callable[[], float],
        tool_loop: Callable[..., object],
        max_tokens: int,
        max_tool_rounds: int,
        panel_size: int = 2,
    ) -> None:
        self.litellm = litellm
        self.fossil = fossil
        self.clock = clock
        self.tool_loop = tool_loop
        self.max_tokens = max_tokens
        self.max_tool_rounds = max_tool_rounds
        self.panel_size = panel_size

    async def run(
        self,
        task: Mapping[str, Any],
        *,
        seats: SeatingManager,
        catalog: Sequence[str],
        workspace: Path,
        panel_root: Path,
    ) -> PipelineResult:
        rungs: list[PipelineRung] = []
        observed = observe_workspace(workspace)
        rungs.append(PipelineRung("observe", True, observed))

        preflight = await default_preflight(self.fossil, str(task.get("prompt") or ""))
        rungs.append(PipelineRung("preflight", bool(preflight.get("ok")), preflight))

        models = select_panel(
            seats, catalog, task=task, now=self.clock(), size=self.panel_size
        )
        rungs.append(
            PipelineRung(
                "seat",
                len(models) >= 2,
                {"models": list(models), "catalog_count": len(catalog)},
            )
        )
        if len(models) < 2:
            return PipelineResult(
                ok=False,
                mode="multi_route",
                rungs=tuple(rungs),
                models=models,
                winner=None,
                winner_workspace=None,
                winner_tool_calls=0,
                errors=("panel_requires_two_live_models",),
            )

        spec = dict(task.get("verification") or {})
        commands = [str(item) for item in spec.get("commands") or () if str(item).strip()]
        required = [str(item) for item in spec.get("required_files") or ()]
        protected = list(spec.get("protected_paths") or []) + ["checker.py"]
        prompt = str(task.get("prompt") or "")
        acceptance = str(task.get("acceptance") or "")
        panel_root.mkdir(parents=True, exist_ok=True)

        def _ignore(_directory: str, names: list[str]) -> set[str]:
            return {
                name
                for name in names
                if name
                in {
                    "runtime-data",
                    "data",
                    "pipeline",
                    ".git",
                    "__pycache__",
                    ".pytest_cache",
                    "receipts.jsonl",
                    "journal.sqlite3",
                }
            }

        async def run_arm(index: int, model: str) -> dict[str, Any]:
            arm = panel_root / f"{index:02d}-{_slug(model)}"
            if arm.exists():
                shutil.rmtree(arm)
            shutil.copytree(workspace, arm, ignore=_ignore, dirs_exist_ok=False)
            executor = ToolExecutor(arm, protected_paths=protected)
            try:
                output, tools_used = await self._isolated_loop(
                    model,
                    prompt,
                    acceptance,
                    executor,
                    int(task.get("max_tokens") or self.max_tokens),
                )
            except Exception as exc:
                return {
                    "model": model,
                    "workspace": str(arm),
                    "ok": False,
                    "checker_passed": False,
                    "error_type": type(exc).__name__,
                    "tool_calls": 0,
                    "output_sha256": None,
                }
            checker_ok = True
            returncode = 0
            checker_error = None
            checker_stderr = ""
            if commands:
                runner = executor.verification_runner()
                runner.authorize_verification(commands)
                checked = runner.execute("run_command", {"command": commands[0]})
                result = checked.get("result") if isinstance(checked, Mapping) else {}
                if not isinstance(result, Mapping):
                    result = {}
                raw_code = result.get("returncode")
                returncode = int(raw_code) if raw_code is not None else 1
                checker_ok = bool(checked.get("ok")) and returncode == 0
                checker_error = checked.get("error")
                checker_stderr = str(result.get("stderr") or "")[:500]
            files_ok = all((arm / relative).is_file() for relative in required)
            digest = hashlib.sha256((output or "").encode()).hexdigest() if output else None
            return {
                "model": model,
                "workspace": str(arm),
                "ok": checker_ok and files_ok,
                "checker_passed": checker_ok,
                "files_ok": files_ok,
                "returncode": returncode,
                "tool_calls": tools_used,
                "output_sha256": digest,
                "error_type": None,
                "checker_error": checker_error,
                "checker_stderr": checker_stderr,
            }

        arms: list[dict[str, Any]] = []
        for i, model in enumerate(models):
            arms.append(await run_arm(i, model))
        passing = [arm for arm in arms if arm.get("ok")]
        distinct = len({arm.get("output_sha256") for arm in passing if arm.get("output_sha256")})
        winner = sorted(
            passing, key=lambda item: (-int(item.get("tool_calls") or 0), item["model"])
        )
        winner_arm = winner[0] if winner else None

        disagreement = len(passing) >= 2 and distinct > 1
        third: dict[str, Any] | None = None
        if disagreement:
            used = {arm["model"] for arm in arms}
            leftover = [model for model in catalog if model not in used]
            if leftover:
                third = await run_arm(len(models), leftover[0])
                arms.append(third)
                if third.get("ok"):
                    winner_arm = third

        rungs.append(
            PipelineRung(
                "fanout",
                bool(passing),
                {
                    "arms": sanitize(arms),
                    "passing": len(passing),
                    "disagreement": disagreement,
                    "third_called": third is not None,
                },
            )
        )
        rungs.append(
            PipelineRung(
                "checker",
                winner_arm is not None,
                {"winner": None if winner_arm is None else winner_arm["model"]},
            )
        )

        if winner_arm is None:
            return PipelineResult(
                ok=False,
                mode="multi_route",
                rungs=tuple(rungs),
                models=models,
                winner=None,
                winner_workspace=None,
                winner_tool_calls=0,
                errors=("no_checker_passing_arm",),
            )

        source = Path(str(winner_arm["workspace"]))
        for relative in required:
            src = source / relative
            if src.is_file():
                dest = workspace / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dest)
        rungs.append(
            PipelineRung(
                "promote_winner",
                True,
                {"winner": winner_arm["model"], "copied": required},
            )
        )
        missing = [rung.name for rung in rungs if not rung.ok]
        return PipelineResult(
            ok=not missing,
            mode="multi_route",
            rungs=tuple(rungs),
            models=tuple(arm["model"] for arm in arms),
            winner=str(winner_arm["model"]),
            winner_workspace=str(source),
            winner_tool_calls=int(winner_arm.get("tool_calls") or 0),
            errors=tuple(f"rung_failed:{name}" for name in missing),
        )

    async def _isolated_loop(
        self,
        model: str,
        prompt: str,
        acceptance: str,
        executor: ToolExecutor,
        max_tokens: int,
    ) -> tuple[str, int]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": (
                    f"{prompt}\n\nAcceptance: {acceptance}\n"
                    f"You are isolated seat {model}. Write only inside this workspace."
                ),
            },
        ]
        advertised = {str(schema.get("function", {}).get("name")) for schema in executor.schemas()}
        tool_calls = 0
        for round_index in range(self.max_tool_rounds + 1):
            completion = await self.litellm.chat_completion(
                model=model,
                messages=messages,
                tools=executor.schemas(),
                stream=True,
                max_tokens=max_tokens,
                temperature=0,
            )
            if not completion.tool_calls:
                return completion.content or "", tool_calls
            if round_index >= self.max_tool_rounds:
                raise RuntimeError("tool round limit exhausted")
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
                result = (
                    executor.execute(call.name, call.arguments)
                    if call.name in advertised
                    else {"ok": False, "error_type": "ToolError", "error": "unadvertised tool"}
                )
                if result.get("ok"):
                    tool_calls += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(sanitize(result), ensure_ascii=False),
                    }
                )
        raise RuntimeError("isolated tool loop terminated unexpectedly")


def _slug(model: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in model)[:40]
