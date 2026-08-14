from pathlib import Path

import pytest

from cortex_v5.arbitration import MultiModelArbitrator
from cortex_v5.contracts import StreamCompletion, ToolCall
from cortex_v5.settings import Settings


class FakeLiteLLM:
    def __init__(self):
        self.calls = []

    async def chat_completion(self, **kwargs):
        self.calls.append(kwargs)
        model = kwargs["model"]
        if not kwargs["tools"]:
            return StreamCompletion(content=f"adjudication for {model}", finish_reason="stop")
        count = sum(1 for call in self.calls if call["model"] == model and call["tools"])
        if count == 1:
            content = (
                "def answer(x):\n    return x + 1\n"
                if model == "good"
                else "def answer(x):\n    return x - 1\n"
            )
            return StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall(
                        f"call-{model}",
                        "write",
                        {"path": "solution.py", "content": content},
                    ),
                ),
                finish_reason="tool_calls",
            )
        return StreamCompletion(content="implemented", finish_reason="stop")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_arbitrator_isolates_candidates_and_uses_checker_for_winner(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        allowed_root=tmp_path,
        litellm_url="http://fake",
        litellm_api_key="secret",
        max_tool_rounds=3,
        default_max_tokens=128,
    )
    fake = FakeLiteLLM()
    root = tmp_path / "arbitration"

    def prepare(workspace: Path):
        (workspace / "checker.py").write_text(
            "from solution import answer\nassert answer(1) == 2\n", encoding="utf-8"
        )

    arbitrator = MultiModelArbitrator(
        settings,
        ["good", "bad"],
        litellm=fake,
        workspace_root=root,
        concurrency=2,
    )
    result = await arbitrator.run(
        "Implement answer in solution.py",
        prepare_workspace=prepare,
        adjudicator_model="good",
        final_workspace=tmp_path / "winner",
    )

    assert result.task_type == "arbitration"
    assert result.winner == "good"
    assert result.adjudicator_called
    assert len(result.candidates) == 2
    assert {candidate.status for candidate in result.candidates} == {"completed", "failed"}
    assert (tmp_path / "winner" / "solution.py").read_text(encoding="utf-8").startswith(
        "def answer"
    )
    assert all(Path(candidate.workspace).is_dir() for candidate in result.candidates)
    assert all("content" not in candidate.to_dict() for candidate in result.candidates)
    await arbitrator.close()
