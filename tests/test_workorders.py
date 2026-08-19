from __future__ import annotations

import copy

import pytest

from cortex_v5.workorders import (
    ReceiptValidationError,
    WorkOrderValidationError,
    advance_after_runner_loss,
    build_fixture_attempt_receipt,
    fanin,
    fanout,
    validate_attempt,
    validate_work_order,
)

BASE_SHA = "1" * 40


def _work_order(*, generation: int = 3, attempt_count: int = 2) -> dict:
    return {
        "schema_version": "cortex.workorder.v1",
        "work_order_id": "wo_cortex02_fixture",
        "repo": "Pukujan/cortex-v5",
        "base_sha": BASE_SHA,
        "objective": "Prove the secretless disposable-runner WorkOrder harness.",
        "acceptance": {
            "commands": ["python -m pytest -q"],
            "required_files": ["cortex_v5/workorders.py"],
        },
        "risk": ["code_change", "disposable_compute"],
        "deadline": "2026-08-20T00:00:00Z",
        "idempotency_key": "CORTEX-02:fixture:1",
        "correlation": {
            "project_issue_id": "#94",
            "task_id": "CORTEX-02",
            "trace_id": "trace_cortex02_fixture",
        },
        "generation": generation,
        "attempt_count": attempt_count,
        "max_parallel": min(attempt_count, 2),
    }


def _completed_receipt(attempt: dict, *, passed: bool = True, model_done: bool = False) -> dict:
    return {
        "schema_version": "cortex.workorder-attempt-receipt.v1",
        "work_order_id": attempt["work_order_id"],
        "attempt_id": attempt["attempt_id"],
        "generation": attempt["generation"],
        "base_sha": attempt["base_sha"],
        "destination": attempt["destination"],
        "started_at": "2026-08-19T21:00:00Z",
        "finished_at": "2026-08-19T21:05:00Z",
        "status": "completed" if passed else "failed",
        "checkpoint": {
            "checkpoint_id": f"checkpoint_{attempt['attempt_id']}",
            "completed_stage_ids": ["checkout", "execute", "verify"],
            "next_stage_id": None,
            "artifact_ref": f"checkpoint/{attempt['attempt_id']}.json",
        },
        "verification": {
            "passed": passed,
            "checks": ["pytest"],
            "errors": [] if passed else ["pytest failed"],
        },
        "model_reported_done": model_done,
    }


def test_work_order_preflight_is_explicit_bounded_and_secretless() -> None:
    order = validate_work_order(_work_order())

    assert order["base_sha"] == BASE_SHA
    assert order["generation"] == 3
    assert order["attempt_count"] == 2
    assert order["max_parallel"] == 2

    bad_sha = _work_order()
    bad_sha["base_sha"] = "main"
    with pytest.raises(WorkOrderValidationError, match="base_sha"):
        validate_work_order(bad_sha)

    too_wide = _work_order(attempt_count=9)
    too_wide["max_parallel"] = 9
    with pytest.raises(WorkOrderValidationError, match="attempt_count"):
        validate_work_order(too_wide)

    secret_bearing = _work_order()
    secret_bearing["metadata"] = {"api_key": "must-not-cross-actions-boundary"}
    with pytest.raises(WorkOrderValidationError, match="secret-bearing"):
        validate_work_order(secret_bearing)


def test_fanout_is_flat_deterministic_and_isolates_patch_destinations() -> None:
    order = _work_order()

    first = fanout(order)
    second = fanout(copy.deepcopy(order))

    assert first == second
    assert len(first) == 2
    assert len({attempt["attempt_id"] for attempt in first}) == 2
    assert len({attempt["destination"] for attempt in first}) == 2
    assert all(attempt["generation"] == order["generation"] for attempt in first)
    assert all(attempt["base_sha"] == BASE_SHA for attempt in first)
    assert all(attempt["deadline"] == order["deadline"] for attempt in first)
    assert all(attempt["destination"].endswith(".patch") for attempt in first)
    assert all("children" not in attempt for attempt in first)


def test_attempt_payload_is_exactly_bound_to_deterministic_fanout() -> None:
    order = _work_order(attempt_count=1)
    attempt = fanout(order)[0]

    assert validate_attempt(order, attempt) == attempt

    tampered = copy.deepcopy(attempt)
    tampered["destination"] = "patches/attacker-controlled.patch"
    with pytest.raises(ReceiptValidationError, match="deterministic fanout"):
        validate_attempt(order, tampered)


def test_fixture_receipt_is_bound_and_non_authoritative_model_metadata() -> None:
    order = _work_order(attempt_count=1)
    attempt = fanout(order)[0]

    receipt = build_fixture_attempt_receipt(
        order,
        attempt,
        passed=True,
        started_at="2026-08-19T21:00:00Z",
        finished_at="2026-08-19T21:00:01Z",
    )

    assert receipt["attempt_id"] == attempt["attempt_id"]
    assert receipt["destination"] == attempt["destination"]
    assert receipt["verification"]["passed"] is True
    assert receipt["model_reported_done"] is False


def test_fanin_uses_mechanical_verification_not_model_authored_done() -> None:
    order = _work_order()
    attempts = fanout(order)

    verified = _completed_receipt(attempts[0], passed=True, model_done=False)
    failed_but_claims_done = _completed_receipt(attempts[1], passed=False, model_done=True)
    closeout = fanin(order, [failed_but_claims_done, verified])

    assert closeout["schema_version"] == "cortex.workorder-closeout.v1"
    assert closeout["status"] == "PASS"
    assert closeout["winning_attempt_id"] == verified["attempt_id"]
    assert closeout["verified_attempt_ids"] == [verified["attempt_id"]]
    assert closeout["model_authored_done_is_authority"] is False


def test_fanin_requires_every_current_generation_attempt_before_closeout() -> None:
    order = _work_order()
    first_attempt = fanout(order)[0]

    with pytest.raises(ReceiptValidationError, match="terminal receipt for every"):
        fanin(order, [_completed_receipt(first_attempt)])


def test_fanin_rejects_duplicate_stale_late_and_mismatched_receipts() -> None:
    order = _work_order()
    attempt = fanout(order)[0]
    receipt = _completed_receipt(attempt)

    with pytest.raises(ReceiptValidationError, match="duplicate"):
        fanin(order, [receipt, copy.deepcopy(receipt)])

    stale = copy.deepcopy(receipt)
    stale["generation"] = order["generation"] - 1
    with pytest.raises(ReceiptValidationError, match="receipt generation"):
        fanin(order, [stale])

    late = copy.deepcopy(receipt)
    late["finished_at"] = "2026-08-20T00:00:01Z"
    with pytest.raises(ReceiptValidationError, match="deadline"):
        fanin(order, [late])

    mismatched = copy.deepcopy(receipt)
    mismatched["base_sha"] = "2" * 40
    with pytest.raises(ReceiptValidationError, match="base_sha"):
        fanin(order, [mismatched])


def test_runner_death_recovery_advances_generation_and_fences_old_receipts() -> None:
    order = _work_order(generation=4, attempt_count=1)
    old_attempt = fanout(order)[0]
    checkpoint_receipt = _completed_receipt(old_attempt, passed=False)
    checkpoint_receipt["status"] = "checkpointed"
    checkpoint_receipt["finished_at"] = "2026-08-19T21:02:00Z"
    checkpoint_receipt["checkpoint"] = {
        "checkpoint_id": "checkpoint_runner_died_after_execute",
        "completed_stage_ids": ["checkout", "execute"],
        "next_stage_id": "verify",
        "artifact_ref": "checkpoint/runner-death.json",
    }

    recovered = advance_after_runner_loss(order, checkpoint_receipt)
    successor = fanout(recovered)[0]

    assert recovered["generation"] == 5
    assert recovered["recovery"] == {
        "checkpoint_id": "checkpoint_runner_died_after_execute",
        "artifact_ref": "checkpoint/runner-death.json",
        "completed_stage_ids": ["checkout", "execute"],
        "next_stage_id": "verify",
    }
    assert successor["attempt_id"] != old_attempt["attempt_id"]
    assert successor["generation"] == 5
    assert successor["skip_stage_ids"] == ["checkout", "execute"]
    assert successor["resume_from_checkpoint_id"] == "checkpoint_runner_died_after_execute"

    stale_success = _completed_receipt(old_attempt, passed=True)
    with pytest.raises(ReceiptValidationError, match="receipt generation"):
        fanin(recovered, [stale_success])


def test_runner_death_recovery_rejects_completed_or_unbound_checkpoint() -> None:
    order = _work_order(generation=2, attempt_count=1)
    attempt = fanout(order)[0]

    completed = _completed_receipt(attempt, passed=True)
    with pytest.raises(ReceiptValidationError, match="checkpointed"):
        advance_after_runner_loss(order, completed)

    wrong_attempt = _completed_receipt(attempt, passed=False)
    wrong_attempt["status"] = "checkpointed"
    wrong_attempt["attempt_id"] = "attempt_not_from_this_work_order"
    with pytest.raises(ReceiptValidationError, match="attempt_id"):
        advance_after_runner_loss(order, wrong_attempt)
