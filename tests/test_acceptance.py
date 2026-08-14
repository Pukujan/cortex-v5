import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from cortex_v5.acceptance import (
    AcceptancePreflightError,
    _acceptance_request,
    _poll_task,
    preflight_acceptance,
    run_humaneval_acceptance,
)
from cortex_v5.contracts import SinkResult
from cortex_v5.journal import Journal
from cortex_v5.litellm import LiteLLMClient
from cortex_v5.observability import EventRecorder
from cortex_v5.settings import Settings


def configured_settings(tmp_path: Path) -> Settings:
    return Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        allowed_root=tmp_path,
        litellm_url="https://litellm.invalid",
        litellm_api_key="super-secret-key",
    )


class FakeJournal:
    instances = []

    def __init__(self, path):
        self.path = Path(path)
        self.closed = False
        type(self).instances.append(self)

    def close(self):
        self.closed = True


class FakeRecorder:
    instances = []
    result = SinkResult(True, True, True)

    def __init__(self, journal, *, repo_root):
        self.journal = journal
        self.repo_root = repo_root
        self.env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "https://grave.invalid",
            "LANGFUSE_HOST": "https://langfuse.invalid",
            "LANGFUSE_PUBLIC_KEY": "public-value",
            "LANGFUSE_SECRET_KEY": "secret-value",
        }
        self.closed = False
        self.records = []
        type(self).instances.append(self)

    def record(self, task_id, event_type, payload):
        self.records.append((task_id, event_type, payload))
        return type(self).result

    def close(self):
        self.closed = True


class FakeLiteLLM:
    instances = []
    models = ("provider/model-b", "provider/model-a")

    def __init__(self, base_url, *, api_key):
        self.base_url = base_url
        self.api_key = api_key
        self.closed = False
        self.refresh_count = 0
        self.chat_count = 0
        type(self).instances.append(self)

    async def refresh_models(self):
        self.refresh_count += 1
        return type(self).models

    async def chat_completion(self, **_kwargs):
        self.chat_count += 1
        raise AssertionError("acceptance preflight must not issue a synthetic chat call")

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_fakes():
    FakeJournal.instances.clear()
    FakeRecorder.instances.clear()
    FakeLiteLLM.instances.clear()
    FakeRecorder.result = SinkResult(True, True, True)
    FakeLiteLLM.models = ("provider/model-b", "provider/model-a")


async def test_preflight_observes_live_catalog_and_all_sinks_then_closes(tmp_path):
    evidence = await preflight_acceptance(
        configured_settings(tmp_path),
        journal_factory=FakeJournal,
        recorder_factory=FakeRecorder,
        litellm_factory=FakeLiteLLM,
    )

    assert evidence["models"]["live"] is True
    assert evidence["models"]["count"] == 2
    assert len(evidence["models"]["catalog_sha256"]) == 64
    assert evidence["telemetry"] == {
        "local_ok": True,
        "gravebuster_ok": True,
        "langfuse_ok": True,
        "event_type": "acceptance.preflight",
    }
    assert all(isinstance(value, bool) for value in evidence["config"].values())
    assert FakeLiteLLM.instances[0].refresh_count == 1
    assert FakeLiteLLM.instances[0].chat_count == 0
    assert FakeRecorder.instances[0].records[0][1] == "acceptance.preflight"
    assert FakeLiteLLM.instances[0].closed
    assert FakeRecorder.instances[0].closed
    assert FakeJournal.instances[0].closed
    serialized = json.dumps(evidence)
    assert "super-secret-key" not in serialized
    assert "secret-value" not in serialized
    assert "https://" not in serialized


async def test_preflight_uses_production_transports_and_journal_with_only_network_mocked(
    tmp_path,
):
    sync_requests = []
    async_requests = []
    resources = {}

    def telemetry_handler(request):
        sync_requests.append(request)
        return httpx.Response(200)

    async def litellm_handler(request):
        async_requests.append(request)
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "live/model"}]})

    def recorder_factory(journal, *, repo_root):
        client = httpx.Client(transport=httpx.MockTransport(telemetry_handler))
        recorder = EventRecorder(
            journal,
            repo_root=repo_root,
            env={
                "OTEL_EXPORTER_OTLP_ENDPOINT": "https://grave.invalid",
                "LANGFUSE_HOST": "https://langfuse.invalid",
                "LANGFUSE_PUBLIC_KEY": "public",
                "LANGFUSE_SECRET_KEY": "secret",
            },
            client=client,
        )
        recorder._owns_client = True
        resources["telemetry_client"] = client
        return recorder

    def litellm_factory(base_url, *, api_key):
        client = httpx.AsyncClient(transport=httpx.MockTransport(litellm_handler))
        litellm = LiteLLMClient(base_url, api_key=api_key, client=client)
        litellm._owned = True
        resources["litellm_client"] = client
        return litellm

    evidence = await preflight_acceptance(
        configured_settings(tmp_path),
        journal_factory=Journal,
        recorder_factory=recorder_factory,
        litellm_factory=litellm_factory,
    )
    assert evidence["models"]["count"] == 1
    assert len(async_requests) == 1
    assert len(sync_requests) == 2
    assert resources["litellm_client"].is_closed
    assert resources["telemetry_client"].is_closed

    with sqlite3.connect(tmp_path / "data" / "journal.sqlite3") as connection:
        rows = connection.execute(
            "SELECT payload FROM records WHERE kind='event' ORDER BY id"
        ).fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["event_type"] == "acceptance.preflight"


async def test_preflight_fails_before_resources_when_required_config_is_missing(tmp_path):
    settings = configured_settings(tmp_path)
    settings = Settings(
        project_root=settings.project_root,
        data_dir=settings.data_dir,
        allowed_root=settings.allowed_root,
        litellm_url=settings.litellm_url,
        litellm_api_key="",
    )
    with pytest.raises(AcceptancePreflightError) as raised:
        await preflight_acceptance(
            settings,
            journal_factory=FakeJournal,
            recorder_factory=FakeRecorder,
            litellm_factory=FakeLiteLLM,
        )
    assert raised.value.phase == "required_v5_config"
    assert not FakeJournal.instances
    assert not FakeLiteLLM.instances


async def test_preflight_rejects_empty_live_catalog_and_closes_resources(tmp_path):
    FakeLiteLLM.models = ()
    with pytest.raises(AcceptancePreflightError) as raised:
        await preflight_acceptance(
            configured_settings(tmp_path),
            journal_factory=FakeJournal,
            recorder_factory=FakeRecorder,
            litellm_factory=FakeLiteLLM,
        )
    assert raised.value.phase == "nonempty_model_catalog"
    assert not FakeRecorder.instances[0].records
    assert FakeLiteLLM.instances[0].closed
    assert FakeRecorder.instances[0].closed
    assert FakeJournal.instances[0].closed


async def test_preflight_rejects_partial_telemetry_observation(tmp_path):
    FakeRecorder.result = SinkResult(True, True, False)
    with pytest.raises(AcceptancePreflightError) as raised:
        await preflight_acceptance(
            configured_settings(tmp_path),
            journal_factory=FakeJournal,
            recorder_factory=FakeRecorder,
            litellm_factory=FakeLiteLLM,
        )
    assert raised.value.phase == "telemetry_observation"
    assert raised.value.evidence["telemetry"]["langfuse_ok"] is False
    assert FakeLiteLLM.instances[0].closed
    assert FakeRecorder.instances[0].closed
    assert FakeJournal.instances[0].closed


def test_model_prompt_uses_public_problem_not_private_checker(tmp_path):
    row = {
        "task_id": "HumanEval/7",
        "prompt": "PUBLIC PROBLEM: return the square.",
        "test": "PRIVATE_SENTINEL_TEST",
        "entry_point": "square",
    }
    request = _acceptance_request(row, tmp_path)
    prompt = request["prompt"]
    assert row["prompt"] in prompt
    assert row["test"] not in prompt
    assert "checker.py" not in prompt
    assert "source.json" not in prompt
    assert "inspect" not in prompt.casefold()
    assert request["verification"]["protected_paths"] == ["checker.py", "source.json"]


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class PollClient:
    def __init__(self, states):
        self.states = iter(states)
        self.run_posts = []

    async def get(self, path):
        assert path == "/v1/tasks/task-1"
        return FakeResponse(next(self.states))

    async def post(self, path, json):
        self.run_posts.append((path, json))
        return FakeResponse({"accepted": True})


async def no_wait(_seconds):
    return None


async def test_polling_triggers_only_once_for_unchanged_eligible_wait_state():
    waiting = {
        "task_id": "task-1",
        "status": "waiting_for_model",
        "generation": 2,
        "attempt_count": 4,
        "next_eligible_at": 10,
        "updated_at": 20,
        "waiting_reason": "backoff",
    }
    client = PollClient(
        [
            waiting,
            {**waiting, "updated_at": 21},
            {**waiting, "updated_at": 22},
            {**waiting, "status": "failed"},
        ]
    )
    latest = await _poll_task(
        client,
        "task-1",
        waiting,
        timeout_seconds=30,
        monotonic=lambda: 0,
        wall_clock=lambda: 10,
        sleeper=no_wait,
    )
    assert latest["status"] == "failed"
    assert client.run_posts == [("/v1/tasks/task-1/run", {})]


async def test_failed_preflight_prevents_live_dataset_fetch(monkeypatch, tmp_path):
    fetched = False

    async def fail_preflight(_settings):
        raise AcceptancePreflightError("telemetry_observation", {})

    async def forbidden_fetch(_task_id):
        nonlocal fetched
        fetched = True
        return {}

    monkeypatch.setattr("cortex_v5.acceptance.preflight_acceptance", fail_preflight)
    monkeypatch.setattr("cortex_v5.acceptance.fetch_humaneval_task", forbidden_fetch)
    with pytest.raises(AcceptancePreflightError):
        await run_humaneval_acceptance(configured_settings(tmp_path), timeout_seconds=1)
    assert fetched is False
