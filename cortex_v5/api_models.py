"""Strict HTTP request models for the local V5 API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class VerificationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commands: list[str] = Field(default_factory=list, max_length=12)
    required_files: list[str] = Field(default_factory=list, max_length=50)
    protected_paths: list[str] = Field(default_factory=list, max_length=50)
    require_output: bool = True
    require_tool_call: bool = False
    require_external_telemetry: bool = False
    real_input_id: str | None = Field(default=None, max_length=200)

    @field_validator("commands", "required_files", "protected_paths")
    @classmethod
    def nonblank_items(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value for value in cleaned):
            raise ValueError("items must not be blank")
        return cleaned


class TaskSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=200_000)
    task_type: str | None = Field(default=None, max_length=80)
    risk: Literal["low", "medium", "high", "critical"] | None = None
    workspace: str | None = Field(default=None, max_length=2048)
    acceptance: str | None = Field(default=None, max_length=50_000)
    max_tokens: int | None = Field(default=None, ge=1, le=100_000)
    metadata: dict[str, Any] = Field(default_factory=dict)
    verification: VerificationSpec = Field(default_factory=VerificationSpec)
    idempotency_key: str | None = Field(default=None, max_length=200)
    issue_id: str | None = Field(default=None, max_length=80)
    issue_state: str | None = Field(default=None, max_length=40)
    models: list[str] = Field(default_factory=list, max_length=8)
    autostart: bool = True


class HumanAnswers(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answers: dict[str, Any] = Field(min_length=1, max_length=30)
    autostart: bool = True


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
