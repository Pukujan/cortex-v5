from pathlib import Path

import pytest

from cortex_v5.contracts import StreamCompletion, ToolCall
from cortex_v5.journal import Journal
from cortex_v5.pipeline import MechanicalPipeline, observe_workspace, requires_multi_route
from cortex_v5.runtime import CortexRuntime
from cortex_v5.seating import SeatingManager
from cortex_v5.settings import Settings
from tests.test_runtime import FakeLiteLLM, FakeRecorder


def test_observe_is_inventory_before_hypothesis(tmp_path: Path):
    (tmp_path / "notes.md").write_text("x", encoding="utf-8")
    observed = observe_workspace(tmp_path)
    assert observed["observation_first"] is True
    assert observed["hypothesis_not_yet_formed"] is True
    assert "notes.md" in observed["files"]


def test_research_and_explicit_models_require_multi_route():
    assert not requires_multi_route({"methodology": {"task_type": "research"}})
    assert requires_multi_route({"methodology": {"methodology_ids": ["M5"]}})
    assert requires_multi_route({"methodology": {"task_type": "arbitration"}})
    assert requires_multi_route({"models": ["a", "b"], "methodology": {"task_type": "build"}})
    assert not requires_multi_route(
        {"methodology": {"task_type": "build", "methodology_ids": ["M3"]}}
    )


@pytest.mark.asyncio
async def test_pipeline_runs_two_isolated_arms_and_promotes_checker_winner(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        allowed_root=tmp_path,
        litellm_url="http://litellm.invalid",
        litellm_api_key="secret",
        max_tool_rounds=3,
        default_max_tokens=128,
    )
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "checker.py").write_text(
        "from pathlib import Path\n"
        "text = Path('report.md').read_text(encoding='utf-8')\n"
        "assert 'winner-mark' in text\n"
        "print('PASS')\n",
        encoding="utf-8",
    )
    lite = FakeLiteLLM(
        [
            StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall(
                        "g1",
                        "write",
                        {"path": "report.md", "content": "winner-mark from good"},
                    ),
                ),
                finish_reason="tool_calls",
            ),
            StreamCompletion(content="good done", finish_reason="stop"),
            StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall("b1", "write", {"path": "report.md", "content": "wrong arm"}),
                ),
                finish_reason="tool_calls",
            ),
            StreamCompletion(content="bad done", finish_reason="stop"),
        ],
        models=("good", "bad"),
    )
    journal = Journal(settings.data_dir)
    runtime = CortexRuntime(
        settings,
        journal=journal,
        recorder=FakeRecorder(journal),
        litellm=lite,
    )
    pipeline = MechanicalPipeline(
        litellm=lite,
        fossil=runtime.fossil,
        clock=runtime.clock,
        tool_loop=runtime._tool_loop,
        max_tokens=128,
        max_tool_rounds=3,
    )
    result = await pipeline.run(
        {
            "task_id": "t1",
            "prompt": "Research two theories and write report.md",
            "acceptance": "checker passes",
            "max_tokens": 128,
            "models": ["good", "bad"],
            "verification": {"commands": ["python checker.py"], "required_files": ["report.md"]},
        },
        seats=SeatingManager(),
        catalog=["good", "bad"],
        workspace=workspace,
        panel_root=tmp_path / "panel",
    )
    assert result.ok
    assert result.winner == "good"
    assert (workspace / "report.md").read_text(encoding="utf-8") == "winner-mark from good"
    names = [rung.name for rung in result.rungs]
    assert names[:4] == ["observe", "preflight", "seat", "fanout"]


@pytest.mark.asyncio
async def test_runtime_research_task_uses_pipeline_not_single_seat(tmp_path: Path):
    settings = Settings(
        project_root=tmp_path,
        data_dir=tmp_path / "data",
        allowed_root=tmp_path,
        litellm_url="http://litellm.invalid",
        litellm_api_key="secret",
        max_tool_rounds=3,
        default_max_tokens=128,
    )
    workspace = tmp_path / "work"
    workspace.mkdir()
    (workspace / "checker.py").write_text(
        "from pathlib import Path\n"
        "assert Path('report.md').read_text(encoding='utf-8').strip()\n"
        "print('PASS')\n",
        encoding="utf-8",
    )
    lite = FakeLiteLLM(
        [
            StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall("a1", "write", {"path": "report.md", "content": "theory A cited"}),
                ),
                finish_reason="tool_calls",
            ),
            StreamCompletion(content="A", finish_reason="stop"),
            StreamCompletion(
                content="",
                tool_calls=(
                    ToolCall("b1", "write", {"path": "report.md", "content": "theory B cited"}),
                ),
                finish_reason="tool_calls",
            ),
            StreamCompletion(content="B", finish_reason="stop"),
        ],
        models=("alpha", "beta"),
    )
    journal = Journal(settings.data_dir)
    runtime = CortexRuntime(
        settings,
        journal=journal,
        recorder=FakeRecorder(journal),
        litellm=lite,
    )
    submitted = await runtime.submit(
        {
            "prompt": "Research cited prior art and write report.md",
            "workspace": str(workspace),
            "acceptance": "checker passes",
            "task_type": "research",
            "models": ["alpha", "beta"],
            "verification": {
                "commands": ["python checker.py"],
                "required_files": ["report.md"],
                "require_tool_call": True,
            },
        }
    )
    result = await runtime.run(submitted["task_id"])
    assert result["status"] == "completed"
    assert result["pipeline"]["mode"] == "multi_route"
    assert result["pipeline"]["winner"] in {"alpha", "beta"}
    assert result["attempt_count"] >= 2
    assert (workspace / "report.md").is_file()
