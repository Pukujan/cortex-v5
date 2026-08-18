from pathlib import Path

import pytest

from cortex_v5.contracts import ModelChoice
from cortex_v5.opencode_dispatch import (
    build_opencode_command,
    canonical_model,
    choose_cross_vendor,
    vendor_for,
)


def choice(model: str, *, eligible_at: float = 0.0, rank: int = 0) -> ModelChoice:
    return ModelChoice(model=model, score=(100 - rank, model), eligible_at=eligible_at)


def test_catalog_prefix_aliases_keep_vendor_identity():
    assert canonical_model("[grok] grok-4.6") == "grok-4.6"
    assert vendor_for("[grok] grok-4.6") == "xai"
    assert vendor_for("gpt-5.6-sol") == "openai"
    assert vendor_for("unknown-model") is None


def test_cross_vendor_selection_skips_duplicates_unknown_and_ineligible():
    ranked = (
        choice("grok-4.6", rank=0),
        choice("[grok] grok-4.6", rank=1),
        choice("unknown-model", rank=2),
        choice("gpt-5.6-sol", rank=3),
        choice("kimi-k3", eligible_at=50.0, rank=4),
        choice("qwen3.8-max", rank=5),
    )
    selected = choose_cross_vendor(ranked, count=3, now=10.0)
    assert tuple(item.model for item in selected) == (
        "grok-4.6",
        "gpt-5.6-sol",
        "qwen3.8-max",
    )


def test_cross_vendor_selection_fails_closed_when_floor_is_unavailable():
    with pytest.raises(RuntimeError, match="requested 2 independent vendor seats"):
        choose_cross_vendor((choice("grok-4.6"),), count=2, now=0.0)


def test_opencode_command_binds_model_workspace_and_packet_without_credentials(tmp_path: Path):
    command = build_opencode_command(
        model="grok-4.6",
        workspace=tmp_path,
        packet="bounded packet",
        title="cortex-test",
    )
    assert command[:3] == ["opencode", "--pure", "run"]
    assert ["--model", "ckff/grok-4.6"] == command[3:5]
    assert str(tmp_path) in command
    assert command[-1] == "bounded packet"
    assert all("secret" not in part.lower() for part in command)


def test_auto_flag_is_explicit_not_default(tmp_path: Path):
    normal = build_opencode_command(
        model="grok-4.6",
        workspace=tmp_path,
        packet="packet",
        title="normal",
    )
    automatic = build_opencode_command(
        model="grok-4.6",
        workspace=tmp_path,
        packet="packet",
        title="auto",
        auto=True,
    )
    assert "--auto" not in normal
    assert "--auto" in automatic
