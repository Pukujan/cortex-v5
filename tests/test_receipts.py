from cortex_v5.receipts import ReceiptStore, decide_skip, sha256_bytes, sha256_paths


def test_missing_receipt_and_open_issue_fail_closed(tmp_path):
    store = ReceiptStore(tmp_path)
    verdict = decide_skip(
        store,
        idempotency_key="k",
        live_inputs_hash="a",
        live_outputs_hash="b",
        issue_state="open",
    )
    assert verdict["decision"] == "open"
    assert "receipt_missing" in verdict["reasons"]
    assert "issue_not_closed" in verdict["reasons"]


def test_matching_receipt_and_closed_issue_skips(tmp_path):
    store = ReceiptStore(tmp_path)
    store.append(
        {
            "idempotency_key": "k",
            "inputs_hash": "in",
            "outputs_hash": "out",
            "issue_state": "closed",
        }
    )
    verdict = decide_skip(
        store,
        idempotency_key="k",
        live_inputs_hash="in",
        live_outputs_hash="out",
        issue_state="closed",
    )
    assert verdict["decision"] == "skip"
    assert verdict["reasons"] == []


def test_hash_mismatch_and_tombstone_reopen(tmp_path):
    store = ReceiptStore(tmp_path)
    store.append({"idempotency_key": "k", "inputs_hash": "old", "outputs_hash": "out"})
    mismatch = decide_skip(
        store,
        idempotency_key="k",
        live_inputs_hash="new",
        live_outputs_hash="out",
        issue_state="closed",
    )
    assert mismatch["decision"] == "open"
    assert "inputs_hash_mismatch" in mismatch["reasons"]

    store.tombstone("k", reason="reopened")
    tombstoned = decide_skip(
        store,
        idempotency_key="k",
        live_inputs_hash="tombstone",
        live_outputs_hash="tombstone",
        issue_state="closed",
    )
    assert tombstoned["decision"] == "open"
    assert "receipt_tombstoned" in tombstoned["reasons"]


def test_workspace_file_hash_is_stable(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    first = sha256_paths(tmp_path, ["a.txt"])
    second = sha256_paths(tmp_path, ["a.txt"])
    assert first == second
    (tmp_path / "a.txt").write_text("changed", encoding="utf-8")
    assert sha256_paths(tmp_path, ["a.txt"]) != first
    assert sha256_bytes(b"x") != sha256_bytes(b"y")
