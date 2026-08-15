"""Isolated multi-model tool-loop arbitration for cross-vendor evaluations."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .acceptance import fetch_humaneval_task
from .litellm import LiteLLMClient
from .methodology import MethodologyEngine
from .observability import sanitize
from .settings import Settings
from .tools import ToolExecutor

WorkspacePreparer = Callable[[Path], None]

_SYSTEM_PROMPT = """You are an independent coding seat in a cross-vendor evaluation.
The user task is the only authority. Work only inside your assigned workspace, use the
advertised tools, inspect before editing, and do not claim completion without the checker.
Never expose credentials or read outside the workspace.
"""


@dataclass(frozen=True)
class CandidateResult:
    model: str
    workspace: str
    status: str
    tool_calls: int
    checker_passed: bool
    returncode: int | None
    output_nonempty: bool
    output_sha256: str | None
    error_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ArbitrationResult:
    task_type: str
    risk: str
    methodology_ids: tuple[str, ...]
    routing_tags: tuple[str, ...]
    candidates: tuple[CandidateResult, ...]
    winner: str | None
    winner_workspace: str | None
    adjudicator_model: str | None
    adjudicator_called: bool
    adjudicator_output_sha256: str | None
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["candidates"] = [item.to_dict() for item in self.candidates]
        return sanitize(result)


def _slug(model: str, index: int) -> str:
    label = re.sub(r"[^a-zA-Z0-9._-]+", "-", model).strip("-")[:48] or "model"
    digest = hashlib.sha256(model.encode()).hexdigest()[:8]
    return f"{index:02d}-{label}-{digest}"


class MultiModelArbitrator:
    """Run independent candidates, verify mechanically, and record a consensus result.

    Candidate workspaces are isolated. The checker, not model prose or an adjudicator's
    recommendation, determines whether a candidate is eligible to win.
    """

    def __init__(
        self,
        settings: Settings,
        models: Sequence[str],
        *,
        litellm: LiteLLMClient | Any | None = None,
        workspace_root: str | Path | None = None,
        max_tokens: int | None = None,
        max_tool_rounds: int | None = None,
        verification_timeout: float | None = None,
        concurrency: int = 2,
    ) -> None:
        cleaned = tuple(dict.fromkeys(str(model).strip() for model in models if str(model).strip()))
        if len(cleaned) < 2:
            raise ValueError("multi-model arbitration requires at least two model IDs")
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        self.settings = settings
        self.models = cleaned
        self.litellm = litellm or LiteLLMClient(
            settings.litellm_url, api_key=settings.litellm_api_key
        )
        self._owned_litellm = litellm is None
        root = Path(workspace_root or settings.project_root / "arbitration-workspace").resolve()
        root.mkdir(parents=True, exist_ok=True)
        root.relative_to(settings.allowed_root.resolve())
        self.workspace_root = root
        self.max_tokens = max_tokens or settings.default_max_tokens
        self.max_tool_rounds = max_tool_rounds or settings.max_tool_rounds
        self.verification_timeout = verification_timeout or 60.0
        self.concurrency = concurrency

    async def close(self) -> None:
        if self._owned_litellm:
            await self.litellm.aclose()

    async def _tool_loop(
        self,
        model: str,
        prompt: str,
        executor: ToolExecutor,
    ) -> tuple[str, int]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
        tool_calls = 0
        advertised = {item["function"]["name"] for item in executor.schemas()}
        for _round in range(self.max_tool_rounds + 1):
            completion = await self.litellm.chat_completion(
                model=model,
                messages=messages,
                tools=executor.schemas(),
                stream=True,
                max_tokens=self.max_tokens,
                temperature=0,
            )
            if not completion.tool_calls:
                return completion.content, tool_calls
            if _round >= self.max_tool_rounds:
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
                tool_calls += 1
                result = (
                    executor.execute(call.name, call.arguments)
                    if call.name in advertised
                    else {"ok": False, "error_type": "ToolError", "error": "unadvertised tool"}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(sanitize(result), ensure_ascii=False),
                    }
                )
        raise RuntimeError("tool loop terminated unexpectedly")

    async def _candidate(
        self,
        index: int,
        model: str,
        prompt: str,
        prepare_workspace: WorkspacePreparer,
        checker_command: str,
        protected_paths: Sequence[str],
    ) -> CandidateResult:
        workspace = self.workspace_root / _slug(model, index)
        workspace.mkdir(parents=True, exist_ok=False)
        prepare_workspace(workspace)
        executor = ToolExecutor(
            workspace,
            protected_paths=list(protected_paths),
            timeout=self.verification_timeout,
        )
        started = time.monotonic()
        try:
            output, tool_calls = await self._tool_loop(model, prompt, executor)
            runner = executor.verification_runner()
            runner.authorize_verification([checker_command])
            checked = runner.execute("run_command", {"command": checker_command})
            result = checked.get("result") if isinstance(checked, Mapping) else None
            returncode = result.get("returncode") if isinstance(result, Mapping) else None
            passed = bool(checked.get("ok")) and returncode == 0
            output_digest = hashlib.sha256(output.encode()).hexdigest() if output else None
            return CandidateResult(
                model=model,
                workspace=str(workspace),
                status="completed" if passed else "failed",
                tool_calls=tool_calls,
                checker_passed=passed,
                returncode=int(returncode) if isinstance(returncode, int) else None,
                output_nonempty=bool(output.strip()),
                output_sha256=output_digest,
            )
        except Exception as exc:
            return CandidateResult(
                model=model,
                workspace=str(workspace),
                status="error",
                tool_calls=0,
                checker_passed=False,
                returncode=None,
                output_nonempty=False,
                output_sha256=None,
                error_type=type(exc).__name__,
            )
        finally:
            del started

    async def _adjudicate(
        self,
        model: str,
        prompt: str,
        candidates: Sequence[CandidateResult],
    ) -> tuple[bool, str | None]:
        summaries = [
            {
                "model": candidate.model,
                "checker_passed": candidate.checker_passed,
                "tool_calls": candidate.tool_calls,
                "status": candidate.status,
            }
            for candidate in candidates
        ]
        try:
            completion = await self.litellm.chat_completion(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a neutral coding adjudicator."},
                    {
                        "role": "user",
                        "content": (
                            "Review these sanitized candidate outcomes for the task. "
                            "Recommend a model ID, but do not override checker failures.\n"
                            + json.dumps(
                                {
                                    "prompt_digest": hashlib.sha256(prompt.encode()).hexdigest(),
                                    "candidates": summaries,
                                }
                            )
                        ),
                    },
                ],
                tools=[],
                stream=True,
                max_tokens=256,
                temperature=0,
            )
            digest = hashlib.sha256((completion.content or "").encode()).hexdigest()
            return True, digest
        except Exception:
            return False, None

    async def run(
        self,
        prompt: str,
        *,
        prepare_workspace: WorkspacePreparer,
        checker_command: str = "python checker.py",
        protected_paths: Sequence[str] = ("checker.py", "source.json"),
        adjudicator_model: str | None = None,
        final_workspace: str | Path | None = None,
    ) -> ArbitrationResult:
        if not prompt.strip():
            raise ValueError("prompt must not be blank")
        started = time.monotonic()
        decision = MethodologyEngine().decide(
            prompt,
            task_type="arbitration",
            risk="medium",
            workspace=str(self.workspace_root),
            acceptance=f"{checker_command} exits successfully for an eligible candidate",
        )
        semaphore = asyncio.Semaphore(self.concurrency)

        async def run_one(index: int, model: str) -> CandidateResult:
            async with semaphore:
                return await self._candidate(
                    index,
                    model,
                    prompt,
                    prepare_workspace,
                    checker_command,
                    protected_paths,
                )

        candidates = tuple(
            await asyncio.gather(*(run_one(i, model) for i, model in enumerate(self.models)))
        )
        passing = [candidate for candidate in candidates if candidate.checker_passed]
        winner_candidate = sorted(
            passing,
            key=lambda item: (-item.tool_calls, item.model),
        )[0] if passing else None
        adjudicator_called = False
        adjudicator_digest: str | None = None
        if adjudicator_model:
            adjudicator_called, adjudicator_digest = await self._adjudicate(
                adjudicator_model, prompt, candidates
            )
        winner_workspace = winner_candidate.workspace if winner_candidate else None
        if winner_candidate and final_workspace:
            destination = Path(final_workspace).resolve()
            destination.mkdir(parents=True, exist_ok=True)
            destination.relative_to(self.settings.allowed_root.resolve())
            source = Path(winner_candidate.workspace) / "solution.py"
            if source.is_file():
                shutil.copyfile(source, destination / "solution.py")
                winner_workspace = str(destination)
        return ArbitrationResult(
            task_type=decision.task_type,
            risk=decision.risk,
            methodology_ids=decision.methodology_ids,
            routing_tags=decision.routing_tags,
            candidates=candidates,
            winner=winner_candidate.model if winner_candidate else None,
            winner_workspace=winner_workspace,
            adjudicator_model=adjudicator_model,
            adjudicator_called=adjudicator_called,
            adjudicator_output_sha256=adjudicator_digest,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )


def prepare_humaneval_workspace(row: Mapping[str, Any]) -> WorkspacePreparer:
    """Build a private checker/source pair for a public HumanEval row."""

    def prepare(workspace: Path) -> None:
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
                    "task_id": row["task_id"],
                    "entry_point": row["entry_point"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    return prepare


async def run_humaneval_arbitration(
    settings: Settings,
    *,
    task_id: str,
    models: Sequence[str],
    adjudicator_model: str | None = None,
    concurrency: int = 2,
) -> dict[str, Any]:
    row = await fetch_humaneval_task(task_id)
    root = settings.project_root / "arbitration-workspace" / task_id.replace("/", "-")
    arbitrator = MultiModelArbitrator(
        settings,
        models,
        workspace_root=root,
        concurrency=concurrency,
    )
    try:
        result = await arbitrator.run(
            (
                f"Solve the public HumanEval task {row['task_id']}. Create a complete Python "
                "module "
                "named solution.py in your workspace. Preserve the required signature and use "
                "the public problem statement below.\n\n"
                f"{row['prompt']}"
            ),
            prepare_workspace=prepare_humaneval_workspace(row),
            checker_command="python checker.py",
            adjudicator_model=adjudicator_model,
            final_workspace=root / "winner",
        )
        payload = result.to_dict()
        payload.update({"acceptance": "public_hugging_face_humaneval", "dataset_task_id": task_id})
        return payload
    finally:
        await arbitrator.close()
