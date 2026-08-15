"""BigCodeBench-Hard benchmark harness (direct verification on the pod).

Runs a v5-style tool loop (read/write/edit/list) against a model, has it write
solution.py, then verifies by running the task's unittest suite directly with
the venv python. Verification runs on the dedicated, isolated pod (the pod is
the sandbox); it does NOT go through v5's production micro-sandbox, which is
incompatible with real third-party test suites.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

from cortex_v5.litellm import LiteLLMClient
from cortex_v5.settings import Settings
from cortex_v5.tools import ToolExecutor

HERE = Path("/home/yoav/bcbharness")
WORKSPACES = HERE / "workspaces"
RESULTS = HERE / "results.json"

SYSTEM_PROMPT = (
    "You are an independent coding seat in a cross-vendor evaluation. "
    "The user task is the only authority. Work only inside your assigned workspace, "
    "use the advertised tools, inspect before editing, and write a complete "
    "solution to solution.py. Never claim completion without the test passing."
)

CHECKER = (
    "import sys, io\n"
    "from pathlib import Path\n"
    "ns = {}\n"
    "exec(Path('solution.py').read_text(encoding='utf-8'), ns)\n"
    "exec(Path('test.py').read_text(encoding='utf-8'), ns)\n"
    "TestCases = ns.get('TestCases')\n"
    "if TestCases is None:\n"
    "    raise RuntimeError('TestCases not found')\n"
    "import unittest\n"
    "buf = io.StringIO()\n"
    "suite = unittest.defaultTestLoader.loadTestsFromTestCase(TestCases)\n"
    "res = unittest.TextTestRunner(stream=buf, verbosity=0).run(suite)\n"
    "sys.stdout.write(buf.getvalue())\n"
    "sys.exit(0 if res.wasSuccessful() else 1)\n"
)


def make_preparer(task: dict):
    instruct = task["instruct_prompt"]
    code = task["code_prompt"]
    test = task["test"]

    def prepare(ws: Path) -> None:
        ws.joinpath("problem.md").write_text(
            instruct
            + "\n\nCreate a complete Python module named solution.py in your workspace that "
            "implements the required function. Use the function signature and imports provided. "
            "Write the entire solution to solution.py and nothing else.\n\n"
            + code,
            encoding="utf-8",
        )
        ws.joinpath("test.py").write_text(test, encoding="utf-8")
        ws.joinpath("checker.py").write_text(CHECKER, encoding="utf-8")
        ws.joinpath("source.json").write_text(
            json.dumps(
                {
                    "benchmark": "bigcodebench-hard",
                    "task_id": task["task_id"],
                    "entry_point": task["entry_point"],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    return prepare


def build_prompt(task: dict) -> str:
    return (
        task["instruct_prompt"]
        + "\n\nComplete the function so that it satisfies the requirements. "
        + "Write your entire solution to solution.py.\n\n"
        + task["code_prompt"]
    )


async def tool_loop(
    client: LiteLLMClient,
    model: str,
    prompt: str,
    executor: ToolExecutor,
    max_rounds: int,
    max_tokens: int,
) -> dict:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    tool_calls = 0
    advertised = {s["function"]["name"] for s in executor.schemas()}
    try:
        for _round in range(max_rounds + 1):
            completion = await client.chat_completion(
                model=model,
                messages=messages,
                tools=executor.schemas(),
                stream=True,
                max_tokens=max_tokens,
                temperature=0,
            )
            if not completion.tool_calls:
                return {"ok": True, "tool_calls": tool_calls, "error": None}
            if _round >= max_rounds:
                return {"ok": False, "tool_calls": tool_calls, "error": "tool_round_limit"}
            assistant_calls = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in completion.tool_calls
            ]
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
                    else {"ok": False, "error": "unadvertised"}
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
        return {"ok": False, "tool_calls": tool_calls, "error": "loop_end"}
    except Exception as exc:  # noqa: BLE001 - record infra failures distinctly
        return {
            "ok": False,
            "tool_calls": tool_calls,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }


def verify(workspace: Path, timeout: float) -> tuple[int, str, str]:
    res = subprocess.run(
        [sys.executable, "checker.py"],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        errors="replace",
    )
    return res.returncode, res.stdout[-3000:], res.stderr[-3000:]


async def run_candidate(
    settings: Settings, model: str, task: dict, timeout: float
) -> dict:
    slug = model.replace("/", "-").replace(":", "-")[:40]
    ws = WORKSPACES / task["task_id"].replace("/", "-") / f"{slug}"
    ws.mkdir(parents=True, exist_ok=False)
    make_preparer(task)(ws)
    executor = ToolExecutor(ws, timeout=timeout)
    client = LiteLLMClient(settings.litellm_url, api_key=settings.litellm_api_key)
    started = time.monotonic()
    try:
        loop = await tool_loop(
            client,
            model,
            build_prompt(task),
            executor,
            settings.max_tool_rounds,
            settings.default_max_tokens,
        )
        if not loop["ok"]:
            return {
                "task_id": task["task_id"], "model": model,
                "checker_passed": False, "tool_calls": loop["tool_calls"],
                "error_type": loop["error"], "elapsed": round(time.monotonic() - started, 1),
            }
        try:
            rc, out, err = verify(ws, timeout)
        except subprocess.TimeoutExpired:
            return {
                "task_id": task["task_id"], "model": model,
                "checker_passed": False, "tool_calls": loop["tool_calls"],
                "error_type": "VERIFY_TIMEOUT", "elapsed": round(time.monotonic() - started, 1),
            }
        return {
            "task_id": task["task_id"], "model": model,
            "checker_passed": rc == 0, "tool_calls": loop["tool_calls"],
            "error_type": None if rc == 0 else "TESTS_FAILED",
            "returncode": rc, "test_out": out, "elapsed": round(time.monotonic() - started, 1),
        }
    finally:
        await client.aclose()


async def run_task(settings, models, task, concurrency, timeout) -> dict:
    sem = asyncio.Semaphore(concurrency)

    async def one(model):
        async with sem:
            return await run_candidate(settings, model, task, timeout)

    cands = await asyncio.gather(*(one(m) for m in models))
    return {"task_id": task["task_id"], "models": {c["model"]: c for c in cands}}


def load_done() -> dict:
    return json.loads(RESULTS.read_text(encoding="utf-8")) if RESULTS.exists() else {}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", help="comma-separated task_ids, or 'all', or 'first:N'")
    ap.add_argument("--models", required=True, help="comma-separated model IDs")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--resume", action="store_true")
    args = ap.parse_args()

    settings = Settings.from_env()
    models = tuple(m.strip() for m in args.models.split(",") if m.strip())
    tasks = json.loads((HERE / "tasks.json").read_text(encoding="utf-8"))

    if args.tasks == "all":
        sel = tasks
    elif args.tasks and args.tasks.startswith("first:"):
        sel = tasks[: int(args.tasks.split(":", 1)[1])]
    elif args.tasks:
        want = set(t for t in args.tasks.split(",") if t)
        sel = [t for t in tasks if t["task_id"] in want]
    else:
        sel = tasks

    done = load_done() if args.resume else {}
    if args.resume:
        sel = [t for t in sel if t["task_id"] not in done]

    print(f"running {len(sel)} tasks x {len(models)} models (concurrency {args.concurrency})", flush=True)
    for task in sel:
        rec = await run_task(settings, models, task, args.concurrency, args.timeout)
        done[rec["task_id"]] = rec
        RESULTS.write_text(json.dumps(done, indent=2), encoding="utf-8")
        passed = sum(1 for c in rec["models"].values() if c["checker_passed"])
        print(f"  {rec['task_id']}: {passed}/{len(models)} passed", flush=True)

    agg = defaultdict(lambda: {"passed": 0, "total": 0, "errors": 0})
    for rec in done.values():
        for c in rec["models"].values():
            agg[c["model"]]["total"] += 1
            if c["checker_passed"]:
                agg[c["model"]]["passed"] += 1
            elif c.get("error_type") and c["error_type"] != "TESTS_FAILED":
                agg[c["model"]]["errors"] += 1
    print("\n=== BigCodeBench-Hard pass rates ===", flush=True)
    for model, st in sorted(
        agg.items(), key=lambda kv: -kv[1]["passed"] / max(1, kv[1]["total"])
    ):
        rate = st["passed"] / max(1, st["total"])
        print(f"  {model:26} {st['passed']:3}/{st['total']:3} ({rate*100:5.1f}%)  infra_errors={st['errors']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))