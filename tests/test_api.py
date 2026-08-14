from pathlib import Path

from fastapi.testclient import TestClient

from cortex_v5.api import create_app
from cortex_v5.contracts import StreamCompletion
from cortex_v5.journal import Journal
from cortex_v5.runtime import CortexRuntime
from cortex_v5.settings import Settings
from tests.test_runtime import FakeLiteLLM, FakeRecorder


def test_http_submit_status_answers_and_auth(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        allowed_root=tmp_path,
        litellm_url="http://litellm.invalid",
        litellm_api_key="secret",
        http_bearer="http-secret",
    )
    journal = Journal(settings.data_dir)
    runtime = CortexRuntime(
        settings,
        journal=journal,
        recorder=FakeRecorder(journal),
        litellm=FakeLiteLLM([StreamCompletion(content="unused")]),
    )
    headers = {"Authorization": "Bearer http-secret"}
    with TestClient(create_app(runtime)) as client:
        assert client.get("/healthz").status_code == 401
        response = client.post(
            "/v1/tasks",
            headers=headers,
            json={
                "prompt": "maybe deploy something",
                "workspace": str(tmp_path),
                "autostart": False,
            },
        )
        assert response.status_code == 202
        task = response.json()
        assert task["status"] == "waiting_for_human"
        status = client.get(f"/v1/tasks/{task['task_id']}", headers=headers)
        assert status.status_code == 200
        answer = client.post(
            f"/v1/tasks/{task['task_id']}/answers",
            headers=headers,
            json={
                "answers": {
                    "scope": "analysis of service A only",
                    "acceptance": "return a report",
                },
                "autostart": False,
            },
        )
        assert answer.status_code == 202
        assert answer.json()["status"] == "ready"
