import pytest

from cortex_v5.methodology import BASE_METHODS, METHODOLOGIES, MethodologyEngine, catalog


def test_catalog_is_complete_ordered_and_canonical_at_boundaries():
    assert len(catalog()) == 34
    assert catalog() is METHODOLOGIES
    assert [item.id for item in catalog()] == [f"M{i}" for i in range(34)]
    assert catalog()[0].title == "Mechanism over memory (the meta-rule)"
    assert catalog()[28].title == "Multi-theory hard debugging (parallel hypothesis fan-out)"
    assert catalog()[33].title == "Cross-runtime debugging replay"


def test_build_pipeline_has_base_build_holdout_and_wiring_methods():
    decision = MethodologyEngine().decide(
        "Implement and wire a local journal", workspace="repo", acceptance="tests pass"
    )
    assert decision.task_type == "build"
    assert decision.risk == "medium"
    assert not decision.ambiguous
    assert set(BASE_METHODS) <= set(decision.methodology_ids)
    assert {"M3", "M4", "M30", "M31"} <= set(decision.methodology_ids)
    assert decision.routing_tags == (
        "task:build",
        "risk:medium",
        "route:build",
        "workspace:bounded",
    )


def test_debugging_is_observation_first_and_deterministic():
    engine = MethodologyEngine()
    kwargs = {"workspace": "repo", "acceptance": "reproduction passes"}
    first = engine.decide("Debug a stalled tool loop and find the root cause", **kwargs)
    second = engine.decide("Debug a stalled tool loop and find the root cause", **kwargs)
    assert first == second
    assert {"M12", "M18", "M28", "M32"} <= set(first.methodology_ids)


def test_migration_selects_replay_and_wiring_methods():
    decision = MethodologyEngine().decide(
        "Migrate the repaired controller to the new runtime with parity",
        workspace="repo",
        acceptance="replay succeeds",
    )
    assert "migration" in decision.task_type
    assert {"M3", "M4", "M30", "M31", "M33"} <= set(decision.methodology_ids)


def test_dispatch_selects_seating_access_and_briefing():
    decision = MethodologyEngine().decide(
        "Summon subagents and dispatch model seats", workspace="repo", acceptance="all report"
    )
    assert decision.task_type == "dispatch"
    assert {"M8", "M11", "M29"} <= set(decision.methodology_ids)


def test_arbitration_selects_cross_vendor_pipeline():
    decision = MethodologyEngine().decide(
        "Run a cross-vendor multi-model arbitration with independent critique",
        workspace="repo",
        acceptance="checker passes",
    )
    assert decision.task_type == "arbitration"
    assert {"M5", "M8", "M19", "M20", "M29", "M30", "M31"} <= set(
        decision.methodology_ids
    )
    assert "route:arbitration" in decision.routing_tags


def test_ambiguity_gate_routes_to_human_with_decision_shaped_questions():
    decision = MethodologyEngine().decide("maybe deploy something")
    assert decision.ambiguous
    assert "route:human" in decision.routing_tags
    assert {"M2", "M24", "M25"} <= set(decision.methodology_ids)
    assert len(decision.questions) >= 3
    assert all(
        "A) Recommended:" in question and "B)" in question and "C)" in question
        for question in decision.questions
    )


def test_answers_and_boundaries_clear_the_gate_without_silent_scope_widening():
    decision = MethodologyEngine().decide(
        "maybe deploy something",
        answers={"scope": "service A only", "acceptance": "health check", "workspace": "repo"},
    )
    assert not decision.ambiguous
    assert decision.questions == ()
    assert "M24" in decision.methodology_ids


def test_explicit_classification_is_honored():
    decision = MethodologyEngine().decide(
        "Handle this item", task_type="research", risk="low", acceptance="report delivered"
    )
    assert decision.task_type == "research"
    assert decision.risk == "low"
    assert {"M21", "M22", "M23", "M25"} <= set(decision.methodology_ids)


def test_invalid_explicit_risk_fails_closed():
    with pytest.raises(ValueError, match="risk must be"):
        MethodologyEngine().decide("Inspect logs", risk="extreme")


def test_catalog_and_decision_do_not_require_legacy_runtime(monkeypatch):
    def deny_import(name, *args, **kwargs):
        if name.startswith(("cortex_core", "cortex_v4")):
            raise AssertionError(f"legacy import attempted: {name}")
        return original_import(name, *args, **kwargs)

    original_import = __import__
    monkeypatch.setattr("builtins.__import__", deny_import)
    decision = MethodologyEngine().decide("Research cited prior art")
    assert decision.task_type == "research"
