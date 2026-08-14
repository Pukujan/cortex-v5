from cortex_v5.seating import SeatingManager


def test_catalog_is_authoritative_and_ranking_is_deterministic():
    seats = SeatingManager()
    first = seats.rank(["other/code-high", "qwen-3.6-max"], task_type="code", risk="high")
    second = seats.rank(["qwen-3.6-max", "other/code-high"], task_type="code", risk="high")
    assert first == second
    assert {choice.model for choice in first} == {"other/code-high", "qwen-3.6-max"}
    assert first[0].model == "other/code-high"


def test_first_real_task_and_after_inactivity_are_probes():
    seats = SeatingManager()
    choice = seats.select(["m"], task_type="x", risk="low", now=10)
    assert choice and choice.is_real_task_probe
    seats.record_result("m", success=True, now=10, was_probe=True)
    assert not seats.select(["m"], task_type="x", risk="low", now=309).is_real_task_probe
    assert seats.select(["m"], task_type="x", risk="low", now=310).is_real_task_probe


def test_probe_switches_on_third_failure_and_success_resets():
    seats = SeatingManager()
    for second in (0, 1):
        transition = seats.record_result(
            "a", success=False, now=second, was_probe=True, candidates=["a", "b"]
        )
        assert transition.action == "retry"
    transition = seats.record_result(
        "a", success=False, now=2, was_probe=True, candidates=["a", "b"]
    )
    assert (transition.action, transition.model) == ("switch", "b")
    seats.record_result("a", success=True, now=3)
    assert seats.states["a"].probe_failures == seats.states["a"].continuous_failures == 0


def test_probe_sequence_persists_without_caller_flag():
    seats = SeatingManager()
    assert seats.record_result("a", success=False, now=0, candidates=["a", "b"]).action == "retry"
    assert seats.record_result("a", success=False, now=1, candidates=["a", "b"]).action == "retry"
    assert seats.record_result("a", success=False, now=2, candidates=["a", "b"]).action == "switch"


def test_switch_preserves_ranked_candidate_order():
    seats = SeatingManager()
    transition = None
    for second in range(3):
        transition = seats.record_result(
            "current",
            success=False,
            now=second,
            was_probe=True,
            candidates=["current", "z-ranked-first", "a-ranked-second"],
        )
    assert transition is not None
    assert (transition.action, transition.model) == ("switch", "z-ranked-first")


def test_continuous_threshold_backoff_and_exhaustion_wait():
    seats = SeatingManager()
    expected = {10: 30, 11: 60, 12: 120, 13: 240, 14: 300, 20: 300}
    for failure in range(1, 21):
        transition = seats.record_result(
            "a", success=False, now=100, was_probe=False, candidates=["a"]
        )
        assert transition.eligible_at == 100 + expected.get(failure, 300 if failure >= 14 else 0)
    assert transition.action == "wait"


def test_state_round_trip_is_json_safe():
    seats = SeatingManager()
    seats.record_result("dynamic/model", success=False, now=7, was_probe=True)
    restored = SeatingManager(state=seats.export_state())
    assert restored.export_state() == seats.export_state()
