import json

import httpx

from cortex_v5.journal import Journal
from cortex_v5.observability import EventRecorder, sanitize


def test_recursive_sanitize():
    clean = sanitize({"nested": [{"api_key": "no"}], "message": "Authorization: Bearer abc.123"})
    assert clean["nested"][0]["api_key"] == "[REDACTED]"
    assert "abc.123" not in clean["message"]


def test_all_sink_outcomes_and_sanitized_local_receipts(tmp_path):
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "https://grave.example",
        "LANGFUSE_HOST": "https://langfuse.example",
        "LANGFUSE_PUBLIC_KEY": "public",
        "LANGFUSE_SECRET_KEY": "private",
    }
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200)

    journal = Journal(tmp_path / "data")
    recorder = EventRecorder(
        journal,
        repo_root=tmp_path,
        env=env,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = recorder.record("t", "tool", {"token": "never persist", "value": 7})
    assert result.acceptance_ready
    assert len(seen) == 2
    events = journal.events("t")
    assert len(events) == 1
    assert "never persist" not in json.dumps(events)
    assert events[0]["payload"]["token"] == "[REDACTED]"
    assert not recorder.jsonl_path.exists()
    assert journal.receipts("t")[0]["local_ok"] is True
    recorder.close()
    journal.close()


def test_remote_failure_is_explicit_and_nonfatal(tmp_path):
    journal = Journal(tmp_path)
    recorder = EventRecorder(journal, repo_root=tmp_path, env={})
    result = recorder.record("t", "created")
    assert result.local_ok and not result.gravebuster_ok and not result.langfuse_ok
    assert not result.acceptance_ready
    recorder.close()
    journal.close()


def test_auxiliary_jsonl_cannot_retain_events_during_idle_runtime(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    legacy_jsonl = data_dir / "events.jsonl"
    legacy_jsonl.write_text(
        json.dumps({"timestamp": 1, "payload": {"value": "legacy"}}) + "\n",
        encoding="utf-8",
    )
    journal = Journal(data_dir, ttl_seconds=60)
    recorder = EventRecorder(journal, repo_root=tmp_path, env={}, jsonl_path=legacy_jsonl)

    # Cleanup is immediate and recording never recreates the redundant store.  The
    # recorder can therefore remain alive and idle for any duration without a second
    # persistent copy surviving the journal TTL.
    assert not legacy_jsonl.exists()
    assert recorder.record("t", "created", {"value": "current"}).local_ok
    assert journal.events("t")[0]["payload"]["value"] == "current"
    assert not legacy_jsonl.exists()

    recorder.close()
    journal.close()


def test_canonical_journal_trace_survives_clean_recorder_restart(tmp_path):
    data_dir = tmp_path / "data"
    journal = Journal(data_dir, ttl_seconds=60)
    recorder = EventRecorder(journal, repo_root=tmp_path, env={})
    owned_client = recorder.client
    assert recorder.record("task", "durable", {"password": "hidden", "value": 3}).local_ok
    recorder.close()
    assert owned_client.is_closed
    journal.close()

    reopened = Journal(data_dir, ttl_seconds=60)
    events = reopened.events("task")
    assert len(events) == 1
    assert events[0]["payload"] == {"password": "[REDACTED]", "value": 3}
    reopened.close()
