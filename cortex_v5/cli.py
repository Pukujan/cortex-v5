"""Command-line entry points for the local Cortex V5 runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence

import uvicorn

from .acceptance import run_humaneval_acceptance
from .api import create_app
from .arbitration import run_humaneval_arbitration
from .settings import Settings


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="cortex-v5")
    commands = root.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the local HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", default=8765, type=int)
    acceptance = commands.add_parser(
        "humaneval", help="run the real public Hugging Face HumanEval acceptance gate"
    )
    acceptance.add_argument("--task-id")
    acceptance.add_argument("--timeout", default=1_800.0, type=float)
    arbitration = commands.add_parser(
        "multimodel", help="run an isolated cross-vendor HumanEval arbitration"
    )
    arbitration.add_argument("--task-id", default="HumanEval/0")
    arbitration.add_argument(
        "--models",
        required=True,
        help="comma-separated live LiteLLM model IDs (at least two)",
    )
    arbitration.add_argument("--adjudicator-model")
    arbitration.add_argument("--concurrency", default=2, type=int)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    settings = Settings.from_env()
    if args.command == "serve":
        if args.host not in {"127.0.0.1", "::1", "localhost"} and not settings.http_bearer:
            raise SystemExit("non-loopback binding requires CORTEX_V5_HTTP_BEARER")
        uvicorn.run(create_app(settings=settings), host=args.host, port=args.port)
        return 0
    if args.command == "multimodel":
        models = tuple(item.strip() for item in args.models.split(",") if item.strip())
        result = asyncio.run(
            run_humaneval_arbitration(
                settings,
                task_id=args.task_id,
                models=models,
                adjudicator_model=args.adjudicator_model,
                concurrency=args.concurrency,
            )
        )
        print(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False))
        return 0 if result.get("winner") else 1
    result = asyncio.run(
        run_humaneval_acceptance(
            settings, human_eval_task_id=args.task_id, timeout_seconds=args.timeout
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
