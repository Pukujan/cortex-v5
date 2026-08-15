"""Append-only execution receipts. Not journal scratch and not FOSSIL knowledge.

A later agent may skip work only when ``decide_skip`` returns ``skip``. Missing
any clause fails closed. Closeout prose is never consulted.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

SkipDecision = Literal["skip", "open"]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def sha256_paths(root: str | Path, relative_paths: list[str] | tuple[str, ...]) -> str:
    """Hash a stable manifest of workspace-relative files."""

    base = Path(root).resolve()
    parts: list[str] = []
    for relative in sorted(relative_paths):
        target = (base / relative).resolve()
        try:
            target.relative_to(base)
        except ValueError as exc:
            raise ValueError(f"receipt path escapes workspace: {relative}") from exc
        digest = sha256_file(target) if target.is_file() else "missing"
        parts.append(f"{relative}:{digest}")
    return sha256_bytes("\n".join(parts).encode("utf-8"))


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    return value


class ReceiptStore:
    """Durable local receipts. These do not expire with the 24-hour journal."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.path = self.data_dir / "receipts.jsonl"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        key = str(receipt.get("idempotency_key") or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        record = {
            "schema": "cortex.v5.execution_receipt.v1",
            "idempotency_key": key,
            "inputs_hash": str(receipt.get("inputs_hash") or ""),
            "outputs_hash": str(receipt.get("outputs_hash") or ""),
            "test_ids": list(receipt.get("test_ids") or []),
            "git_sha": receipt.get("git_sha"),
            "issue_id": receipt.get("issue_id"),
            "issue_state": str(receipt.get("issue_state") or ""),
            "task_id": receipt.get("task_id"),
            "tombstoned": bool(receipt.get("tombstoned")),
            "origin": str(receipt.get("origin") or "runtime"),
            "created_at": float(receipt.get("created_at") or time.time()),
            "detail": _safe(dict(receipt.get("detail") or {})),
        }
        if not record["inputs_hash"] or not record["outputs_hash"]:
            raise ValueError("inputs_hash and outputs_hash are required")
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        return record

    def tombstone(self, idempotency_key: str, *, reason: str = "") -> dict[str, Any]:
        return self.append(
            {
                "idempotency_key": idempotency_key,
                "inputs_hash": "tombstone",
                "outputs_hash": "tombstone",
                "tombstoned": True,
                "origin": "tombstone",
                "detail": {"reason": reason},
            }
        )

    def all(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        with self._lock, self.path.open(encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                records.append(json.loads(text))
        return records

    def latest(self, idempotency_key: str) -> dict[str, Any] | None:
        matches = [item for item in self.all() if item.get("idempotency_key") == idempotency_key]
        return matches[-1] if matches else None


def decide_skip(
    store: ReceiptStore,
    *,
    idempotency_key: str,
    live_inputs_hash: str,
    live_outputs_hash: str,
    issue_state: str | None,
) -> dict[str, Any]:
    """Return a fail-closed skip verdict. Closeout text is not an input."""

    receipt = store.latest(idempotency_key)
    reasons: list[str] = []
    if receipt is None:
        reasons.append("receipt_missing")
    else:
        if receipt.get("tombstoned"):
            reasons.append("receipt_tombstoned")
        if receipt.get("inputs_hash") != live_inputs_hash:
            reasons.append("inputs_hash_mismatch")
        if receipt.get("outputs_hash") != live_outputs_hash:
            reasons.append("outputs_hash_mismatch")
    normalized_issue = (issue_state or "").strip().lower()
    if normalized_issue != "closed":
        reasons.append("issue_not_closed")
    decision: SkipDecision = "skip" if not reasons else "open"
    return {
        "decision": decision,
        "reasons": reasons,
        "receipt": receipt,
        "authority": "execution_receipt",
    }
