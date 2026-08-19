"""Small shared contracts used across the independent V5 control plane."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    SUBMITTED = "submitted"
    WAITING_FOR_HUMAN = "waiting_for_human"
    READY = "ready"
    RUNNING = "running"
    WAITING_FOR_MODEL = "waiting_for_model"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True)
class MethodologyDecision:
    task_type: str
    risk: str
    methodology_ids: tuple[str, ...]
    ambiguous: bool = False
    questions: tuple[str, ...] = ()
    routing_tags: tuple[str, ...] = ()
    rationale: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelChoice:
    model: str
    score: tuple[Any, ...]
    reasons: tuple[str, ...] = ()
    is_real_task_probe: bool = False
    eligible_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StreamCompletion:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None
    termination: str = "connection_closed"
    event_count: int = 0
    response_id: str | None = None
    model: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SinkResult:
    local_ok: bool
    gravebuster_ok: bool
    langfuse_ok: bool
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def acceptance_ready(self) -> bool:
        return self.local_ok and self.gravebuster_ok and self.langfuse_ok

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["acceptance_ready"] = self.acceptance_ready
        return result
