"""Deterministic live-catalog model seating and retry mechanics.

Seating score, highest first: ``(available, tag_overlap, -tier, success - failure,
success, model)``.  ``tier`` is the research-grounded priority from
``docs/MODEL-SEATING-RESEARCH-2026-08.md`` (measured BigCodeBench-Hard results,
per-model sources, and the cross-vendor rationale).  It is a documented prior, not
an undocumented hard-coded list: availability and task-tag relevance outrank it,
and the backoff/switch thresholds below override a persistently failing model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final

from .contracts import ModelChoice

INACTIVITY_PROBE_SECONDS = 300.0
PROBE_FAILURE_LIMIT = 3
CONTINUOUS_FAILURE_LIMIT = 20

# Research-grounded seat priority (lower index = higher priority).  Provenance and
# per-model evidence: docs/MODEL-SEATING-RESEARCH-2026-08.md sections 2-4.  The
# approved cross-vendor frontier starter is positions 0-4 (xAI, OpenAI, Moonshot,
# Alibaba, Google); the remainder follows the consolidated public-benchmark ranking.
# Catalog prefix duplicates (e.g. "[aws]deepseek-v3.2") normalize to the unprefixed
# name via _tier().  Models absent from this tuple share _DEFAULT_TIER.
MODEL_TIERS: Final[tuple[str, ...]] = (
    "grok-4.6",
    "gpt-5.6-sol",
    "kimi-k3",
    "qwen3.8-max",
    "gemini-3.6-flash",
    "gpt-5.5",
    "gpt-5.6-terra",
    "gemini-3.5-flash",
    "gemini-3.5-flash-high",
    "gpt-5.6-luna",
    "glm-5.2",
    "glm-5.2-metered",
    "glm-5",
    "minimax-m3",
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "mimo-v2.5-pro",
    "kimi-k2.7-code",
    "kimi-k2-thinking",
    "gemini-3.1-pro-preview",
    "qwen3.6-plus",
    "glm-5-turbo",
    "minimax-m2.5",
    "glm-4.7",
    "deepseek-v3.2",
    "gemini-3.1-flash-lite-preview",
    "qwen3-coder-next",
    "grok-4.5",
    "qwen-3.6-max",
    "gemini-3.1-pro-preview-search",
    "gemini-3.5-flash-search",
    "gemini-3.1-flash-lite-image",
    "qwen3.7-flash",
)
_DEFAULT_TIER = len(MODEL_TIERS)


@dataclass
class ModelState:
    probe_failures: int = 0
    continuous_failures: int = 0
    last_activity: float | None = None
    eligible_at: float = 0.0
    successes: int = 0
    failures: int = 0
    probe_active: bool = False


@dataclass(frozen=True)
class SeatTransition:
    action: str
    model: str | None
    eligible_at: float
    is_real_task_probe: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _tags(model: str) -> set[str]:
    return {part for part in model.lower().replace("/", "-").replace("_", "-").split("-") if part}


def _tier(model: str) -> int:
    """Research-grounded priority index (lower = higher priority).

    Catalog prefix duplicates (``[aws]deepseek-v3.2``, ``[grok] grok-4.6``) map to
    their unprefixed tier entry; anything unknown shares ``_DEFAULT_TIER``.
    """
    normalized = model.lower().strip()
    if "[" in normalized and "]" in normalized:
        normalized = normalized.split("]", 1)[1].strip()
    return MODEL_TIERS.index(normalized) if normalized in MODEL_TIERS else _DEFAULT_TIER


class SeatingManager:
    def __init__(self, *, state: Mapping[str, Any] | None = None) -> None:
        self.states: dict[str, ModelState] = {}
        self.outcomes: dict[str, dict[str, int]] = {}
        if state:
            self.import_state(state)

    def rank(
        self,
        models: Sequence[str],
        *,
        task_type: str,
        risk: str,
        methodology_tags: Sequence[str] = (),
        now: float = 0.0,
    ) -> tuple[ModelChoice, ...]:
        desired = {task_type.lower(), risk.lower(), *map(str.lower, methodology_tags)}
        choices: list[ModelChoice] = []
        for model in sorted(set(models)):
            state = self.states.setdefault(model, ModelState())
            outcome = self.outcomes.get(model, {})
            overlap = len(desired & _tags(model))
            success = outcome.get("success", state.successes)
            failure = outcome.get("failure", state.failures)
            available = state.eligible_at <= now
            score = (int(available), overlap, -_tier(model), success - failure, success, model)
            probe = (
                state.probe_active
                or state.last_activity is None
                or now - state.last_activity >= INACTIVITY_PROBE_SECONDS
            )
            choices.append(ModelChoice(model, score, ("live_catalog",), probe, state.eligible_at))
        return tuple(sorted(choices, key=lambda choice: choice.score, reverse=True))

    def select(self, models: Sequence[str], **criteria: Any) -> ModelChoice | None:
        now = float(criteria.get("now", 0.0))
        return next(
            (choice for choice in self.rank(models, **criteria) if choice.eligible_at <= now), None
        )

    @staticmethod
    def _backoff(failures: int) -> float:
        if failures < 10:
            return 0.0
        return min(30.0 * (2 ** (failures - 10)), 300.0)

    def record_result(
        self,
        model: str,
        *,
        success: bool,
        now: float,
        was_probe: bool | None = None,
        candidates: Sequence[str] = (),
    ) -> SeatTransition:
        state = self.states.setdefault(model, ModelState())
        probe = (
            (
                state.probe_active
                or state.last_activity is None
                or now - state.last_activity >= INACTIVITY_PROBE_SECONDS
            )
            if was_probe is None
            else was_probe
        )
        state.last_activity = now
        bucket = self.outcomes.setdefault(model, {"success": 0, "failure": 0})
        if success:
            state.probe_failures = state.continuous_failures = 0
            state.probe_active = False
            state.eligible_at = now
            state.successes += 1
            bucket["success"] += 1
            return SeatTransition("continue", model, now, probe, "success_reset_counters")

        state.failures += 1
        bucket["failure"] += 1
        state.continuous_failures += 1
        if probe:
            state.probe_failures += 1
            state.probe_active = state.probe_failures < PROBE_FAILURE_LIMIT
        state.eligible_at = now + self._backoff(state.continuous_failures)
        must_switch = (
            probe and state.probe_failures >= PROBE_FAILURE_LIMIT
        ) or state.continuous_failures >= CONTINUOUS_FAILURE_LIMIT
        if must_switch:
            eligible: list[str] = []
            seen: set[str] = set()
            for candidate in candidates:
                if candidate in seen:
                    continue
                seen.add(candidate)
                if (
                    candidate != model
                    and self.states.setdefault(candidate, ModelState()).eligible_at <= now
                ):
                    eligible.append(candidate)
            if eligible:
                return SeatTransition("switch", eligible[0], now, True, "failure_threshold")
            next_at = min(
                (self.states[c].eligible_at for c in candidates if c != model),
                default=max(state.eligible_at, now + INACTIVITY_PROBE_SECONDS),
            )
            return SeatTransition("wait", None, next_at, True, "candidates_exhausted")
        return SeatTransition("retry", model, state.eligible_at, probe, "new_attempt_required")

    def export_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "models": {name: asdict(state) for name, state in sorted(self.states.items())},
            "outcomes": {name: dict(value) for name, value in sorted(self.outcomes.items())},
        }

    def import_state(self, data: Mapping[str, Any]) -> None:
        self.states = {
            str(name): ModelState(**dict(value))
            for name, value in dict(data.get("models", {})).items()
        }
        self.outcomes = {
            str(name): {str(k): int(v) for k, v in dict(value).items()}
            for name, value in dict(data.get("outcomes", {})).items()
        }


SeatManager = SeatingManager
