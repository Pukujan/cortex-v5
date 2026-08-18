"""Temporary OpenCode execution shell driven by V5 methodology and seating policy.

This module does not make OpenCode authoritative Cortex state.  It exists only as an
operational bridge while the native V5 coding-agent loop is unstable: a human or
upstream controller supplies an already-granulated task packet, V5 decides
methodology/risk and model seating, and OpenCode executes the selected seat through
the configured LiteLLM-compatible endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Final

from .contracts import MethodologyDecision, ModelChoice
from .litellm import LiteLLMClient
from .methodology import MethodologyEngine
from .seating import SeatingManager
from .settings import Settings

_PROVIDER_ID = "ckff"
_MUTATING_ROLES: Final[frozenset[str]] = frozenset({"worker", "test-writer"})
_VENDOR_BY_MODEL: Final[dict[str, str]] = {
    "grok-4.6": "xai",
    "gpt-5.6-sol": "openai",
    "kimi-k3": "moonshot",
    "qwen3.8-max": "alibaba",
    "gemini-3.6-flash": "google",
}


def canonical_model(model: str) -> str:
    """Normalize catalog prefix aliases to the model name used by MODEL_TIERS."""
    normalized = model.lower().strip()
    if "[" in normalized and "]" in normalized:
        normalized = normalized.split("]", 1)[1].strip()
    return normalized


def vendor_for(model: str) -> str | None:
    """Return the known independent vendor for a qualified frontier seat."""
    return _VENDOR_BY_MODEL.get(canonical_model(model))


def choose_cross_vendor(
    ranked: tuple[ModelChoice, ...], *, count: int, now: float
) -> tuple[ModelChoice, ...]:
    """Choose up to ``count`` eligible qualified seats from distinct vendors."""
    if count < 1:
        raise ValueError("count must be positive")
    chosen: list[ModelChoice] = []
    vendors: set[str] = set()
    for choice in ranked:
        if choice.eligible_at > now:
            continue
        vendor = vendor_for(choice.model)
        if vendor is None or vendor in vendors:
            continue
        chosen.append(choice)
        vendors.add(vendor)
        if len(chosen) == count:
            break
    if len(chosen) != count:
        raise RuntimeError(
            f"requested {count} independent vendor seats but only {len(chosen)} are eligible"
        )
    return tuple(chosen)


def build_opencode_command(
    *,
    model: str,
    workspace: Path,
    packet: str,
    title: str,
    provider_id: str = _PROVIDER_ID,
    auto: bool = False,
) -> list[str]:
    """Build one foreground OpenCode invocation without embedding credentials."""
    command = [
        "opencode",
        "--pure",
        "run",
        "--model",
        f"{provider_id}/{model}",
        "--dir",
        str(workspace),
        "--format",
        "json",
        "--title",
        title,
    ]
    if auto:
        command.append("--auto")
    command.append(packet)
    return command


def _packet_prompt(
    packet: str,
    *,
    role: str,
    decision: MethodologyDecision,
    model: str,
    packet_hash: str,
) -> str:
    methods = ", ".join(decision.methodology_ids)
    return (
        "CORTEX GRANULATED EXECUTION PACKET\n"
        f"role: {role}\n"
        f"model-seat: {model}\n"
        f"task-type: {decision.task_type}\n"
        f"risk: {decision.risk}\n"
        f"methodologies: {methods}\n"
        f"packet-sha256: {packet_hash}\n\n"
        "Authority rules:\n"
        "- The packet below is the complete authorized granule; do not widen scope.\n"
        "- Current repository state wins over stale historical context.\n"
        "- Stop rather than invent missing authority or acceptance meaning.\n"
        "- Produce workspace/research evidence; do not claim Cortex completion.\n\n"
        f"{packet.strip()}\n"
    )


def _workspace(settings: Settings, value: Path) -> Path:
    workspace = value.expanduser().resolve(strict=True)
    if not workspace.is_dir():
        raise ValueError("workspace must be an existing directory")
    try:
        workspace.relative_to(settings.allowed_root)
    except ValueError as exc:
        raise ValueError("workspace escapes CORTEX_V5_ALLOWED_ROOT") from exc
    return workspace


def _write_receipt(settings: Settings, receipt: dict[str, object], packet_hash: str) -> Path:
    directory = settings.data_dir / "opencode-dispatch"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = int(time.time())
    path = directory / f"{stamp}-{packet_hash[:12]}.json"
    path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


async def _rank(
    settings: Settings,
    decision: MethodologyDecision,
    *,
    now: float,
) -> tuple[ModelChoice, ...]:
    async with LiteLLMClient(
        settings.litellm_url,
        api_key=settings.litellm_api_key,
    ) as client:
        catalog = await client.refresh_models()
    return SeatingManager().rank(
        catalog,
        task_type=decision.task_type,
        risk=decision.risk,
        methodology_tags=decision.routing_tags,
        now=now,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dispatch one pre-granulated task through OpenCode using V5 policy."
    )
    parser.add_argument("packet", type=Path, help="UTF-8 task packet file")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--acceptance", required=True)
    parser.add_argument(
        "--role",
        choices=("worker", "test-writer", "reviewer", "researcher", "evaluator"),
        default="worker",
    )
    parser.add_argument("--task-type")
    parser.add_argument("--risk", choices=("low", "medium", "high", "critical"))
    parser.add_argument("--seats", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--provider-id", default=_PROVIDER_ID)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if not settings.litellm_url or not settings.litellm_api_key:
        raise RuntimeError("V5-local LiteLLM configuration is incomplete")
    workspace = _workspace(settings, args.workspace)
    packet_text = args.packet.read_text(encoding="utf-8")
    if not packet_text.strip():
        raise ValueError("task packet must not be blank")
    packet_hash = hashlib.sha256(packet_text.encode("utf-8")).hexdigest()

    decision = MethodologyEngine().decide(
        packet_text,
        task_type=args.task_type,
        risk=args.risk,
        workspace=str(workspace),
        acceptance=args.acceptance,
    )
    if decision.ambiguous:
        print(
            json.dumps(
                {
                    "status": "waiting_for_human",
                    "questions": decision.questions,
                    "methodology": decision.to_dict(),
                },
                indent=2,
            )
        )
        return 2
    if args.seats > 1 and args.role in _MUTATING_ROLES:
        raise ValueError(
            "multiple mutating seats require isolated worktrees; dispatch one worker/test-writer "
            "seat per workspace"
        )

    now = time.time()
    ranked = await _rank(settings, decision, now=now)
    choices = choose_cross_vendor(ranked, count=args.seats, now=now)
    config = (
        args.config.expanduser().resolve()
        if args.config
        else settings.project_root / "opencode.example.json"
    )
    if not config.is_file():
        raise FileNotFoundError(f"OpenCode config not found: {config}")

    environment = os.environ.copy()
    environment["LITELLM_URL"] = settings.litellm_url
    environment["LITELLM_MASTER_KEY"] = settings.litellm_api_key
    environment["OPENCODE_CONFIG"] = str(config)

    plans: list[dict[str, object]] = []
    commands: list[list[str]] = []
    for index, choice in enumerate(choices, start=1):
        vendor = vendor_for(choice.model)
        title = f"cortex-{args.role}-{packet_hash[:8]}-{index}"
        prompt = _packet_prompt(
            packet_text,
            role=args.role,
            decision=decision,
            model=choice.model,
            packet_hash=packet_hash,
        )
        command = build_opencode_command(
            model=choice.model,
            workspace=workspace,
            packet=prompt,
            title=title,
            provider_id=args.provider_id,
            auto=args.auto,
        )
        commands.append(command)
        plans.append(
            {
                "seat": index,
                "model": choice.model,
                "vendor": vendor,
                "score": list(choice.score),
                "probe": choice.is_real_task_probe,
            }
        )

    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "packet_sha256": packet_hash,
                    "workspace": str(workspace),
                    "methodology": decision.to_dict(),
                    "seats": plans,
                    "commands": [command[:-1] + ["<granulated-packet>"] for command in commands],
                    "config": str(config),
                },
                indent=2,
            )
        )
        return 0

    if shutil.which("opencode") is None:
        raise RuntimeError("opencode executable was not found on PATH")

    results: list[dict[str, object]] = []
    failed = False
    for plan, command in zip(plans, commands, strict=True):
        completed = subprocess.run(command, env=environment, check=False)
        returncode = int(completed.returncode)
        failed = failed or returncode != 0
        results.append({**plan, "returncode": returncode})

    receipt: dict[str, object] = {
        "kind": "opencode_cross_vendor_dispatch",
        "created_at": time.time(),
        "packet_sha256": packet_hash,
        "workspace": str(workspace),
        "role": args.role,
        "acceptance": args.acceptance,
        "methodology": decision.to_dict(),
        "provider_id": args.provider_id,
        "litellm_url": settings.litellm_url,
        "results": results,
    }
    receipt_path = _write_receipt(settings, receipt, packet_hash)
    print(json.dumps({"receipt": str(receipt_path), "results": results}, indent=2))
    return 1 if failed else 0


def main() -> int:
    """CLI entry point for ``python -m cortex_v5.opencode_dispatch``."""
    args = _parser().parse_args()
    try:
        return asyncio.run(_main_async(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"opencode dispatch failed: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
