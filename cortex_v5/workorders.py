"""Secretless disposable-runner WorkOrder contracts for Cortex V5.

GitHub Actions is a replaceable transport/execution surface, not the V5 runtime
or durable truth.  These helpers validate bounded WorkOrders, derive isolated
attempt destinations, mechanically fence receipts, and model runner-loss
recovery by advancing a generation from an immutable checkpoint.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

WORK_ORDER_VERSION = "cortex.workorder.v1"
ATTEMPT_VERSION = "cortex.workorder-attempt.v1"
ATTEMPT_RECEIPT_VERSION = "cortex.workorder-attempt-receipt.v1"
CLOSEOUT_VERSION = "cortex.workorder-closeout.v1"
_MAX_ATTEMPTS = 8
_MAX_PARALLEL = 4
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_WORK_ORDER_ID_RE = re.compile(r"^wo_[A-Za-z0-9_.-]+$")
_SENSITIVE_KEY_RE = re.compile(
    r"authorization|cookie|api[_-]?key|token|secret|password|credential", re.I
)
_TERMINAL_RECEIPT_STATUSES = frozenset({"completed", "failed"})


class WorkOrderValidationError(ValueError):
    """A WorkOrder cannot safely enter the disposable-runner control plane."""


class ReceiptValidationError(ValueError):
    """An attempt receipt cannot contribute to the current WorkOrder generation."""


def _parse_timestamp(value: Any, field: str, error_type: type[ValueError]) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{field} must be a non-empty RFC3339 timestamp")
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError as exc:
        raise error_type(f"{field} must be a valid RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise error_type(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _require_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkOrderValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _require_string_list(value: Any, field: str, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list):
        raise WorkOrderValidationError(f"{field} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WorkOrderValidationError(f"{field} entries must be non-empty strings")
        result.append(item.strip())
    if not result and not allow_empty:
        raise WorkOrderValidationError(f"{field} must not be empty")
    if len(result) != len(set(result)):
        raise WorkOrderValidationError(f"{field} entries must be unique")
    return result


def _find_sensitive_key(value: Any, path: tuple[str, ...] = ()) -> str | None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            text = str(key)
            if _SENSITIVE_KEY_RE.search(text):
                return ".".join((*path, text))
            found = _find_sensitive_key(child, (*path, text))
            if found:
                return found
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found = _find_sensitive_key(child, (*path, str(index)))
            if found:
                return found
    return None


def validate_work_order(values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one bounded, secretless WorkOrder."""

    if not isinstance(values, Mapping):
        raise WorkOrderValidationError("work order must be an object")
    candidate = copy.deepcopy(dict(values))
    if candidate.get("schema_version") != WORK_ORDER_VERSION:
        raise WorkOrderValidationError(f"schema_version must be {WORK_ORDER_VERSION}")

    sensitive_path = _find_sensitive_key(candidate)
    if sensitive_path:
        raise WorkOrderValidationError(
            f"secret-bearing field is forbidden at the Actions boundary: {sensitive_path}"
        )

    work_order_id = _require_string(candidate.get("work_order_id"), "work_order_id")
    if not _WORK_ORDER_ID_RE.fullmatch(work_order_id):
        raise WorkOrderValidationError(
            "work_order_id must start with wo_ and contain only path-safe characters"
        )
    candidate["work_order_id"] = work_order_id
    candidate["repo"] = _require_string(candidate.get("repo"), "repo")
    candidate["objective"] = _require_string(candidate.get("objective"), "objective")
    candidate["idempotency_key"] = _require_string(
        candidate.get("idempotency_key"), "idempotency_key"
    )

    base_sha = _require_string(candidate.get("base_sha"), "base_sha").lower()
    if not _SHA_RE.fullmatch(base_sha):
        raise WorkOrderValidationError("base_sha must be an exact 40-character git SHA")
    candidate["base_sha"] = base_sha

    deadline = _require_string(candidate.get("deadline"), "deadline")
    _parse_timestamp(deadline, "deadline", WorkOrderValidationError)
    candidate["deadline"] = deadline

    acceptance = candidate.get("acceptance")
    if not isinstance(acceptance, Mapping):
        raise WorkOrderValidationError("acceptance must be an object")
    candidate["acceptance"] = {
        "commands": _require_string_list(acceptance.get("commands"), "acceptance.commands"),
        "required_files": _require_string_list(
            acceptance.get("required_files", []),
            "acceptance.required_files",
            allow_empty=True,
        ),
    }
    candidate["risk"] = _require_string_list(candidate.get("risk"), "risk")

    correlation = candidate.get("correlation")
    if not isinstance(correlation, Mapping):
        raise WorkOrderValidationError("correlation must be an object")
    candidate["correlation"] = {
        field: _require_string(correlation.get(field), f"correlation.{field}")
        for field in ("project_issue_id", "task_id", "trace_id")
    }

    generation = candidate.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise WorkOrderValidationError("generation must be a positive integer")
    candidate["generation"] = generation

    attempt_count = candidate.get("attempt_count")
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or not 1 <= attempt_count <= _MAX_ATTEMPTS
    ):
        raise WorkOrderValidationError(
            f"attempt_count must be between 1 and {_MAX_ATTEMPTS}"
        )
    candidate["attempt_count"] = attempt_count

    max_parallel = candidate.get("max_parallel")
    if (
        not isinstance(max_parallel, int)
        or isinstance(max_parallel, bool)
        or not 1 <= max_parallel <= min(_MAX_PARALLEL, attempt_count)
    ):
        raise WorkOrderValidationError(
            f"max_parallel must be between 1 and min({_MAX_PARALLEL}, attempt_count)"
        )
    candidate["max_parallel"] = max_parallel

    recovery = candidate.get("recovery")
    if recovery is not None:
        if not isinstance(recovery, Mapping):
            raise WorkOrderValidationError("recovery must be an object")
        completed = _require_string_list(
            recovery.get("completed_stage_ids", []),
            "recovery.completed_stage_ids",
            allow_empty=True,
        )
        checkpoint_id = _require_string(recovery.get("checkpoint_id"), "recovery.checkpoint_id")
        artifact_ref = _require_string(recovery.get("artifact_ref"), "recovery.artifact_ref")
        next_stage_id = recovery.get("next_stage_id")
        if not isinstance(next_stage_id, str) or not next_stage_id.strip():
            raise WorkOrderValidationError("recovery.next_stage_id must be a non-empty string")
        if next_stage_id.strip() in completed:
            raise WorkOrderValidationError("recovery.next_stage_id must not already be completed")
        candidate["recovery"] = {
            "checkpoint_id": checkpoint_id,
            "artifact_ref": artifact_ref,
            "completed_stage_ids": completed,
            "next_stage_id": next_stage_id.strip(),
        }

    return candidate


def _attempt_id(order: Mapping[str, Any], index: int) -> str:
    material = "\x1f".join(
        (
            str(order["work_order_id"]),
            str(order["generation"]),
            str(index),
            str(order["base_sha"]),
            str(order["idempotency_key"]),
        )
    )
    return f"attempt_{hashlib.sha256(material.encode('utf-8')).hexdigest()[:24]}"


def fanout(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Derive a flat deterministic matrix of isolated patch-producing attempts."""

    order = validate_work_order(values)
    recovery = order.get("recovery") or {}
    completed_stage_ids = list(recovery.get("completed_stage_ids") or [])
    checkpoint_id = recovery.get("checkpoint_id")
    attempts: list[dict[str, Any]] = []
    for index in range(order["attempt_count"]):
        attempt_id = _attempt_id(order, index)
        destination = (
            f"patches/{order['work_order_id']}/g{order['generation']}/"
            f"a{index + 1}-{attempt_id}.patch"
        )
        attempts.append(
            {
                "schema_version": ATTEMPT_VERSION,
                "work_order_id": order["work_order_id"],
                "attempt_id": attempt_id,
                "attempt_index": index,
                "generation": order["generation"],
                "repo": order["repo"],
                "base_sha": order["base_sha"],
                "objective": order["objective"],
                "acceptance": copy.deepcopy(order["acceptance"]),
                "risk": list(order["risk"]),
                "deadline": order["deadline"],
                "correlation": copy.deepcopy(order["correlation"]),
                "destination": destination,
                "skip_stage_ids": completed_stage_ids,
                "resume_from_checkpoint_id": checkpoint_id,
            }
        )
    return attempts


def _validate_receipt_binding(
    order: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    allow_checkpointed: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected_attempts = {item["attempt_id"]: item for item in fanout(order)}
    if not isinstance(receipt, Mapping):
        raise ReceiptValidationError("attempt receipt must be an object")
    candidate = copy.deepcopy(dict(receipt))
    if candidate.get("schema_version") != ATTEMPT_RECEIPT_VERSION:
        raise ReceiptValidationError(
            f"receipt schema_version must be {ATTEMPT_RECEIPT_VERSION}"
        )
    attempt_id = candidate.get("attempt_id")
    if not isinstance(attempt_id, str) or attempt_id not in expected_attempts:
        raise ReceiptValidationError("attempt_id is not bound to this WorkOrder generation")
    expected = expected_attempts[attempt_id]

    exact_fields = {
        "work_order_id": order["work_order_id"],
        "generation": order["generation"],
        "base_sha": order["base_sha"],
        "destination": expected["destination"],
    }
    for field, expected_value in exact_fields.items():
        if candidate.get(field) != expected_value:
            raise ReceiptValidationError(f"receipt {field} does not match the WorkOrder")

    status = candidate.get("status")
    allowed_statuses = set(_TERMINAL_RECEIPT_STATUSES)
    if allow_checkpointed:
        allowed_statuses.add("checkpointed")
    if status not in allowed_statuses:
        expected_status = "terminal or checkpointed" if allow_checkpointed else "terminal"
        raise ReceiptValidationError(f"receipt status must be {expected_status}")

    started = _parse_timestamp(candidate.get("started_at"), "started_at", ReceiptValidationError)
    finished = _parse_timestamp(
        candidate.get("finished_at"), "finished_at", ReceiptValidationError
    )
    deadline = _parse_timestamp(order["deadline"], "deadline", ReceiptValidationError)
    if finished < started:
        raise ReceiptValidationError("receipt finished_at precedes started_at")
    if finished > deadline:
        raise ReceiptValidationError("receipt finished_at exceeds the WorkOrder deadline")

    checkpoint = candidate.get("checkpoint")
    if not isinstance(checkpoint, Mapping):
        raise ReceiptValidationError("receipt checkpoint must be an object")
    checkpoint_id = checkpoint.get("checkpoint_id")
    artifact_ref = checkpoint.get("artifact_ref")
    if not isinstance(checkpoint_id, str) or not checkpoint_id.strip():
        raise ReceiptValidationError("checkpoint_id must be non-empty")
    if not isinstance(artifact_ref, str) or not artifact_ref.strip():
        raise ReceiptValidationError("checkpoint artifact_ref must be non-empty")
    completed = checkpoint.get("completed_stage_ids")
    if not isinstance(completed, list) or any(
        not isinstance(item, str) or not item.strip() for item in completed
    ):
        raise ReceiptValidationError("checkpoint completed_stage_ids must be a string list")
    if len(completed) != len(set(completed)):
        raise ReceiptValidationError("checkpoint completed_stage_ids must be unique")
    next_stage_id = checkpoint.get("next_stage_id")
    if next_stage_id is not None and (
        not isinstance(next_stage_id, str) or not next_stage_id.strip()
    ):
        raise ReceiptValidationError("checkpoint next_stage_id must be null or non-empty")
    if isinstance(next_stage_id, str) and next_stage_id in completed:
        raise ReceiptValidationError("checkpoint next_stage_id must not already be completed")

    verification = candidate.get("verification")
    if not isinstance(verification, Mapping) or not isinstance(verification.get("passed"), bool):
        raise ReceiptValidationError("receipt verification.passed must be boolean")
    checks = verification.get("checks")
    errors = verification.get("errors")
    if not isinstance(checks, list) or any(not isinstance(item, str) for item in checks):
        raise ReceiptValidationError("receipt verification.checks must be a string list")
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        raise ReceiptValidationError("receipt verification.errors must be a string list")

    return candidate, expected


def fanin(values: Mapping[str, Any], receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Validate current-generation receipts and produce a mechanical terminal closeout."""

    order = validate_work_order(values)
    expected_attempts = fanout(order)
    if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
        raise ReceiptValidationError("receipts must be a sequence")

    validated: dict[str, dict[str, Any]] = {}
    for receipt in receipts:
        candidate, _expected = _validate_receipt_binding(
            order, receipt, allow_checkpointed=False
        )
        attempt_id = str(candidate["attempt_id"])
        if attempt_id in validated:
            raise ReceiptValidationError(f"duplicate attempt receipt: {attempt_id}")
        validated[attempt_id] = candidate

    expected_ids = [attempt["attempt_id"] for attempt in expected_attempts]
    missing = [attempt_id for attempt_id in expected_ids if attempt_id not in validated]
    if missing:
        raise ReceiptValidationError(
            "fanin requires a terminal receipt for every current-generation attempt"
        )

    verified_ids = [
        attempt_id
        for attempt_id in expected_ids
        if bool(validated[attempt_id]["verification"]["passed"])
    ]
    winner = verified_ids[0] if verified_ids else None
    return {
        "schema_version": CLOSEOUT_VERSION,
        "work_order_id": order["work_order_id"],
        "generation": order["generation"],
        "base_sha": order["base_sha"],
        "correlation": copy.deepcopy(order["correlation"]),
        "status": "PASS" if winner else "FAILED",
        "winning_attempt_id": winner,
        "verified_attempt_ids": verified_ids,
        "attempt_receipt_ids": expected_ids,
        "model_authored_done_is_authority": False,
    }


def advance_after_runner_loss(
    values: Mapping[str, Any], checkpoint_receipt: Mapping[str, Any]
) -> dict[str, Any]:
    """Advance the generation from a bound checkpoint after disposable-runner loss."""

    order = validate_work_order(values)
    receipt, _attempt = _validate_receipt_binding(
        order, checkpoint_receipt, allow_checkpointed=True
    )
    if receipt["status"] != "checkpointed":
        raise ReceiptValidationError(
            "runner-loss recovery requires a checkpointed, non-terminal receipt"
        )
    checkpoint = receipt["checkpoint"]
    next_stage_id = checkpoint.get("next_stage_id")
    if not isinstance(next_stage_id, str) or not next_stage_id.strip():
        raise ReceiptValidationError("checkpointed receipt requires a next_stage_id")

    recovered = copy.deepcopy(order)
    recovered["generation"] = int(order["generation"]) + 1
    recovered["recovery"] = {
        "checkpoint_id": str(checkpoint["checkpoint_id"]),
        "artifact_ref": str(checkpoint["artifact_ref"]),
        "completed_stage_ids": list(checkpoint["completed_stage_ids"]),
        "next_stage_id": next_stage_id,
    }
    return validate_work_order(recovered)


def _read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise WorkOrderValidationError(f"{path} must contain a JSON object")
    return value


def _write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Small file-oriented CLI used by secretless Actions wrappers."""

    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight")
    preflight.add_argument("work_order")
    preflight.add_argument("--output", required=True)

    expand = subparsers.add_parser("fanout")
    expand.add_argument("work_order")
    expand.add_argument("--output", required=True)

    collect = subparsers.add_parser("fanin")
    collect.add_argument("work_order")
    collect.add_argument("receipts", nargs="+")
    collect.add_argument("--output", required=True)

    recover = subparsers.add_parser("recover")
    recover.add_argument("work_order")
    recover.add_argument("checkpoint_receipt")
    recover.add_argument("--output", required=True)

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "preflight":
        result = validate_work_order(_read_json(args.work_order))
    elif args.command == "fanout":
        result = fanout(_read_json(args.work_order))
    elif args.command == "fanin":
        result = fanin(
            _read_json(args.work_order),
            [_read_json(path) for path in args.receipts],
        )
    else:
        result = advance_after_runner_loss(
            _read_json(args.work_order), _read_json(args.checkpoint_receipt)
        )
    _write_json(args.output, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ATTEMPT_RECEIPT_VERSION",
    "ATTEMPT_VERSION",
    "CLOSEOUT_VERSION",
    "ReceiptValidationError",
    "WORK_ORDER_VERSION",
    "WorkOrderValidationError",
    "advance_after_runner_loss",
    "fanin",
    "fanout",
    "validate_work_order",
]
