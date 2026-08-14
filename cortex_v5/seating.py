"""Deterministic live-catalog model seating and retry mechanics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .contracts import ModelChoice

INACTIVITY_PROBE_SECONDS = 300.0
PROBE_FAILURE_LIMIT = 3
CONTINUOUS_FAILURE_LIMIT = 20
PREFERENCE_HINTS = ("grok-4.5", "qwen-3.6-max", "kimi-k3")


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
            preference = next(
                (
                    len(PREFERENCE_HINTS) - i
                    for i, hint in enumerate(PREFERENCE_HINTS)
                    if hint in model.lower()
                ),
                0,
            )
            available = state.eligible_at <= now
            score = (int(available), overlap, success - failure, success, preference, model)
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
