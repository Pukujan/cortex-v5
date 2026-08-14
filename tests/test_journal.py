import sqlite3
import time

from cortex_v5.journal import Journal


def test_restart_update_records_and_model_state(tmp_path):
    journal = Journal(tmp_path)
    journal.put("t", {"status": "new"})
    journal.update("t", {"status": "running"})
    journal.append_event("t", {"kind": "started"})
    journal.append_receipt("t", {"Authorization": "Bearer cannot-store-this"})
    journal.set_model_state("t", {"attempt": 2})
    journal.append_outcome("t", {"model": "m", "ok": True})
    journal.close()

    restarted = Journal(tmp_path)
    assert restarted.get("t") == {"status": "running"}
    assert restarted.events("t") == [{"kind": "started"}]
    assert restarted.receipts("t") == [{"Authorization": "[REDACTED]"}]
    assert restarted.get_model_state("t") == {"attempt": 2}
    assert restarted.outcomes("t") == [{"model": "m", "ok": True}]


def test_strict_expiry_purges_all_tables(tmp_path):
    journal = Journal(tmp_path, ttl_seconds=0.03)
    journal.put("t", {})
    journal.append_event("t", {})
    journal.set_model_state("t", {})
    time.sleep(0.05)
    assert journal.get("t") is None
    assert journal.events("t") == []
    assert journal.get_model_state("t") is None
    journal.purge_expired()
    with sqlite3.connect(journal.path) as db:
        assert db.execute("SELECT count(*) FROM records").fetchone()[0] == 0


def test_updates_do_not_renew_original_expiry(tmp_path):
    journal = Journal(tmp_path, ttl_seconds=0.08)
    journal.put("t", {"n": 1})
    journal.set_model_state("t", {"n": 1})
    time.sleep(0.05)
    journal.put("t", {"n": 2})
    journal.set_model_state("t", {"n": 2})
    time.sleep(0.05)
    assert journal.get("t") is None
    assert journal.get_model_state("t") is None


def test_expired_global_model_state_can_begin_a_fresh_ttl_window(tmp_path):
    journal = Journal(tmp_path, ttl_seconds=0.03)
    journal.set_model_state("global", {"generation": 1})
    time.sleep(0.05)
    assert journal.get_model_state("global") is None

    journal.set_model_state("global", {"generation": 2})

    assert journal.get_model_state("global") == {"generation": 2}
