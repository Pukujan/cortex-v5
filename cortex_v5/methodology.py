"""Deterministic, V5-local methodology classification and ambiguity gating.

This module deliberately contains its policy data locally.  Runtime decisions never
consult an older Cortex checkout, a corpus, or an external model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from cortex_v5.contracts import MethodologyDecision


@dataclass(frozen=True)
class Methodology:
    id: str
    title: str


_TITLES: Final[tuple[str, ...]] = (
    "Mechanism over memory (the meta-rule)",
    "SEARCH_BRAIN pre-flight (research-first)",
    "Owner elicitation (decision-shaped questions)",
    "The P4 build lane (how every kernel module was built)",
    "Sealed holdout verification",
    "Multi-model arbitration (produce → independent critique → adjudicate)",
    "Governed contract amendment + freeze",
    "Closeout + capture discipline",
    "Model dispatch procedure",
    "Measured-not-guessed benchmarking",
    "Honest debt + provenance",
    "Subagent briefing (dispatch prompts)",
    "Blocked-state protocol",
    "Fact-class routing (answer every question from the RIGHT authority)",
    "Fresh-observation reporting (trust chains terminate at observation)",
    "Handoff reconciliation sweep (handoffs are hypotheses, not state)",
    "One-step refutation before consequential sends (the pre-mortem grep)",
    "Convenience-gradient audit (drift is an ergonomics bug)",
    "Error metabolism (every caught error becomes a mechanism, same day)",
    "Per-model rubric calibration (judges) — every tier-list model (Claude seats frozen off)",
    "Oracle minting (deterministic checkers + hard gold)",
    "Deep audit sweep",
    "Deep research + citation discipline",
    "Querying Cortex efficiently (the search craft)",
    "Question–answer gates (elicitation craft, RUBRIC-SHAPED)",
    "The resolver-choice gate (research vs measure vs derive vs ask)",
    "Legible output (human-language documents + answers, RUBRIC-SHAPED)",
    "Owner-legible diagrams (vertical / mobile-first)",
    "Multi-theory hard debugging (parallel hypothesis fan-out)",
    "Seat access-control matrix (box model + forced-RAG per seat)",
    "Wiring check (end-to-end from the origin, every unit)",
    "Declare the wire BEFORE you build (the other half of M30)",
    "Observation-first root-cause debugging",
    "Cross-runtime debugging replay",
)

METHODOLOGIES: Final[tuple[Methodology, ...]] = tuple(
    Methodology(f"M{index}", title) for index, title in enumerate(_TITLES)
)

BASE_METHODS: Final[tuple[str, ...]] = ("M0", "M1", "M7", "M10", "M26")

# Ordered rules make multi-match classification reproducible.  More specialized
# classes precede broad implementation/research classes.
_TYPE_RULES: Final[tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]] = (
    (
        "arbitration",
        (
            "arbitrate",
            "arbitration",
            "consensus",
            "cross-vendor",
            "cross vendor",
            "multi-model",
            "multi model",
            "independent critique",
            "adjudicate",
        ),
        ("M5", "M8", "M19", "M20", "M29", "M30", "M31"),
    ),
    (
        "debug",
        ("debug", "failure", "failing", "stall", "bug", "repair", "root cause", "root-cause"),
        ("M12", "M18", "M28", "M32"),
    ),
    (
        "evaluation",
        ("eval", "evaluate", "holdout", "oracle", "benchmark", "score", "verify", "verification"),
        ("M4", "M9", "M19", "M20"),
    ),
    (
        "dispatch",
        ("dispatch", "summon", "subagent", "seat", "model route", "fleet", "orchestrat"),
        ("M8", "M11", "M29"),
    ),
    (
        "migration",
        ("migrate", "migration", "port to", "cross-runtime", "parity"),
        ("M3", "M4", "M30", "M31", "M33"),
    ),
    (
        "build",
        ("build", "implement", "create", "add", "wire", "plugin", "refactor", "change", "fix"),
        ("M3", "M4", "M30", "M31"),
    ),
    (
        "research",
        ("research", "survey", "citation", "prior art", "investigate", "look up"),
        ("M13", "M14", "M21", "M22", "M23", "M25"),
    ),
    ("audit", ("audit", "review", "inspect", "assess"), ("M16", "M21", "M23", "M25")),
    ("amendment", ("contract", "amend", "freeze", "governance", "policy"), ("M2", "M6", "M16")),
    ("diagram", ("diagram", "flowchart", "architecture map", "visualize"), ("M27",)),
)

_EXPLICIT_METHODS: Final[dict[str, tuple[str, ...]]] = {
    name: methods for name, _tokens, methods in _TYPE_RULES
}

_HIGH_RISK = (
    "delete",
    "drop",
    "destroy",
    "production",
    "deploy",
    "publish",
    "push",
    "merge",
    "credential",
    "secret",
    "security",
    "payment",
    "billing",
    "contract",
    "permission",
)
_MEDIUM_RISK = ("build", "implement", "change", "edit", "write", "fix", "migrate", "refactor")
_AMBIGUOUS = re.compile(r"\b(?:tbd|unspecified|unclear|whatever|something|somehow|maybe)\b", re.I)


def catalog() -> tuple[Methodology, ...]:
    """Return the immutable, complete local M0..M33 catalog."""

    return METHODOLOGIES


def _ordered_unique(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _classify_type(text: str, explicit: str | None) -> tuple[str, tuple[str, ...]]:
    if explicit:
        normalized = explicit.strip().lower().replace("-", "_")
        aliases = {"eval": "evaluation", "verify": "evaluation", "implementation": "build"}
        normalized = aliases.get(normalized, normalized)
        return normalized, _EXPLICIT_METHODS.get(normalized, ())
    lowered = text.lower()
    matches = [
        (name, methods)
        for name, tokens, methods in _TYPE_RULES
        if any(t in lowered for t in tokens)
    ]
    if not matches:
        return "generic", ()
    return "+".join(name for name, _ in matches), _ordered_unique(
        [method for _name, methods in matches for method in methods]
    )


def _classify_risk(text: str, explicit: str | None) -> str:
    if explicit:
        normalized = explicit.strip().lower()
        if normalized not in {"low", "medium", "high", "critical"}:
            raise ValueError("risk must be one of: low, medium, high, critical")
        return normalized
    lowered = text.lower()
    if any(token in lowered for token in _HIGH_RISK):
        return "high"
    if any(token in lowered for token in _MEDIUM_RISK):
        return "medium"
    return "low"


def _unresolved_questions(
    prompt: str,
    *,
    risk: str,
    answers: dict[str, object],
    workspace: str | None,
    acceptance: str | None,
) -> tuple[str, ...]:
    questions: list[str] = []
    substantive_answers = {str(k): v for k, v in answers.items() if v not in (None, "", [], {})}
    vague = len(prompt.split()) < 3 or bool(_AMBIGUOUS.search(prompt))
    fork = (
        bool(re.search(r"\b(?:or|either)\b", prompt, re.I)) and "choice" not in substantive_answers
    )
    if vague and "scope" not in substantive_answers:
        questions.append(
            "Which scope should I execute? A) Recommended: name the exact deliverable "
            "and boundaries; "
            "B) exploration only (no changes); C) stop this task."
        )
    if fork:
        questions.append(
            "Which stated alternative do you authorize? A) Recommended: choose one option "
            "explicitly; "
            "B) authorize a reversible comparison only; C) stop without executing either option."
        )
    high_risk = risk in {"high", "critical"}
    if high_risk and not acceptance and "acceptance" not in substantive_answers:
        questions.append(
            "What is the completion gate? A) Recommended: provide an observable acceptance check; "
            "B) approve a dry run only; C) do not execute the high-risk work."
        )
    if high_risk and not workspace and "workspace" not in substantive_answers:
        questions.append(
            "Where is the authorized boundary? A) Recommended: name the exact workspace/target; "
            "B) analysis only with no writes; C) stop the task."
        )
    return tuple(questions)


class MethodologyEngine:
    """Pure deterministic policy engine; the human remains the only authority."""

    def decide(
        self,
        prompt: str,
        *,
        task_type: str | None = None,
        risk: str | None = None,
        answers: dict | None = None,
        workspace: str | None = None,
        acceptance: str | None = None,
    ) -> MethodologyDecision:
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        normalized_prompt = " ".join(prompt.split())
        classified_type, selected = _classify_type(normalized_prompt, task_type)
        classified_risk = _classify_risk(normalized_prompt, risk)
        questions = _unresolved_questions(
            normalized_prompt,
            risk=classified_risk,
            answers=answers or {},
            workspace=workspace,
            acceptance=acceptance,
        )
        methods = list(BASE_METHODS)
        methods.extend(selected)
        if questions:
            methods.extend(("M2", "M24", "M25"))
        elif answers:
            methods.append("M24")
        if classified_risk in {"high", "critical"}:
            methods.append("M16")

        tags = [f"task:{classified_type}", f"risk:{classified_risk}"]
        tags.append("route:human" if questions else f"route:{classified_type.split('+')[0]}")
        if workspace:
            tags.append("workspace:bounded")
        rationale = [
            f"Deterministic task classification: {classified_type}.",
            f"Risk classification: {classified_risk}.",
        ]
        if questions:
            rationale.append("Execution is gated until the human resolves consequential ambiguity.")
        else:
            rationale.append("No unresolved consequential fork was detected.")
        return MethodologyDecision(
            task_type=classified_type,
            risk=classified_risk,
            methodology_ids=_ordered_unique(methods),
            ambiguous=bool(questions),
            questions=questions,
            routing_tags=tuple(tags),
            rationale=tuple(rationale),
        )
