from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def _workflow(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_workorder_actions_portfolio_is_reusable_secretless_and_least_privilege() -> None:
    expected = {
        "workorder-preflight.yml": "python -m cortex_v5.workorders preflight",
        "workorder-fanout.yml": "python -m cortex_v5.workorders fanout",
        "workorder-attempt.yml": "python -m cortex_v5.workorders fixture-receipt",
        "workorder-fanin.yml": "python -m cortex_v5.workorders fanin",
        "runner-death-chaos.yml": "test_runner_death_recovery",
    }

    for filename, required_command in expected.items():
        text = _workflow(filename)
        assert "workflow_call:" in text
        assert "permissions:\n  contents: read" in text
        assert "pull_request_target" not in text
        assert "secrets: inherit" not in text
        assert "${{ secrets." not in text
        assert "actions/checkout@v4" in text
        assert required_command in text


def test_fanout_exports_flat_matrix_and_bounded_parallelism() -> None:
    text = _workflow("workorder-fanout.yml")

    assert "matrix:" in text
    assert "max_parallel:" in text
    assert "children" not in text


def test_attempt_is_fixture_only_and_emits_receipt_before_failure() -> None:
    text = _workflow("workorder-attempt.yml")

    assert "fixture_executor_only" in text
    assert "tests/test_workorders.py" in text
    assert "actions/upload-artifact@v4" in text
    assert "workorder-receipt-" in text
    assert "Emit failure after receipt" in text
    assert "acceptance.commands" not in text


def test_fanin_downloads_receipts_and_never_uses_model_done_as_gate() -> None:
    text = _workflow("workorder-fanin.yml")

    assert "actions/download-artifact@v4" in text
    assert "workorder-receipt-*" in text
    assert "model_reported_done" not in text


def test_runner_death_chaos_is_deterministic_and_secretless() -> None:
    text = _workflow("runner-death-chaos.yml")

    assert "workflow_dispatch:" in text
    assert "test_runner_death_recovery_advances_generation_and_fences_old_receipts" in text
    assert "test_runner_death_recovery_rejects_completed_or_unbound_checkpoint" in text


def test_secretless_harness_composes_preflight_matrix_attempts_fanin_and_recovery() -> None:
    text = _workflow("workorder-harness.yml")

    assert "uses: ./.github/workflows/workorder-preflight.yml" in text
    assert "uses: ./.github/workflows/workorder-fanout.yml" in text
    assert "uses: ./.github/workflows/workorder-attempt.yml" in text
    assert "uses: ./.github/workflows/workorder-fanin.yml" in text
    assert "uses: ./.github/workflows/runner-death-chaos.yml" in text
    assert "max-parallel: ${{ fromJSON(needs.fanout.outputs.max_parallel) }}" in text
    assert "matrix: ${{ fromJSON(needs.fanout.outputs.matrix) }}" in text
    assert "attempt_json: ${{ toJSON(matrix) }}" in text
    assert "model_authored_done_is_authority" in text
    assert "pull_request_target" not in text
    assert "${{ secrets." not in text
