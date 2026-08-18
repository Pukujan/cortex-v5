"""Configuration resolved exclusively from the V5 process and V5-local ``.env``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _first(env: Mapping[str, str], *names: str, default: str = "") -> str:
    for name in names:
        value = env.get(name)
        if value and value.strip():
            return value.strip()
    return default


def _integer(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "runtime-data"
    allowed_root: Path = PROJECT_ROOT
    litellm_url: str = ""
    litellm_api_key: str = field(default="", repr=False)
    http_bearer: str = field(default="", repr=False)
    max_attempts: int = 24
    max_tool_rounds: int = 20
    default_max_tokens: int = 4096
    # Maximum interval without streamed network data before V5 aborts the model
    # transport.  This is not a total task wall-clock timeout.  The 600 second
    # default matches the preferred ckff route's operator-published network window.
    model_inactivity_seconds: int = 600

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        project_root: str | Path | None = None,
    ) -> Settings:
        root = Path(project_root or PROJECT_ROOT).resolve()
        if env is None:
            values = {
                key: str(value)
                for key, value in dotenv_values(root / ".env").items()
                if value is not None
            }
        else:
            values = env

        data_dir = Path(
            _first(values, "CORTEX_V5_DATA_DIR", default=str(root / "runtime-data"))
        ).expanduser()
        if not data_dir.is_absolute():
            data_dir = root / data_dir

        allowed_root = Path(
            _first(values, "CORTEX_V5_ALLOWED_ROOT", default=str(root))
        ).expanduser()
        if not allowed_root.is_absolute():
            allowed_root = root / allowed_root

        return cls(
            project_root=root,
            data_dir=data_dir.resolve(),
            allowed_root=allowed_root.resolve(),
            litellm_url=_first(
                values,
                "CORTEX_V5_LITELLM_URL",
                "LITELLM_URL",
                "litellm_url",
            ).rstrip("/"),
            litellm_api_key=_first(
                values,
                "CORTEX_V5_LITELLM_API_KEY",
                "LITELLM_MASTER_KEY",
                "litellm_master_key",
                "LITELLM_PROXY_KEY",
                "litellm_virtual_key",
            ),
            http_bearer=_first(values, "CORTEX_V5_HTTP_BEARER"),
            max_attempts=max(1, _integer(values, "CORTEX_V5_MAX_ATTEMPTS", 24)),
            max_tool_rounds=max(1, _integer(values, "CORTEX_V5_MAX_TOOL_ROUNDS", 20)),
            default_max_tokens=max(1, _integer(values, "CORTEX_V5_DEFAULT_MAX_TOKENS", 4096)),
            model_inactivity_seconds=max(
                1, _integer(values, "CORTEX_V5_MODEL_INACTIVITY_SECONDS", 600)
            ),
        )

    def public(self) -> dict[str, object]:
        """Return non-secret operational configuration for health/status responses."""
        return {
            "project_root": str(self.project_root),
            "data_dir": str(self.data_dir),
            "allowed_root": str(self.allowed_root),
            "litellm_configured": bool(self.litellm_url and self.litellm_api_key),
            "http_auth_enabled": bool(self.http_bearer),
            "max_attempts": self.max_attempts,
            "max_tool_rounds": self.max_tool_rounds,
            "default_max_tokens": self.default_max_tokens,
            "model_inactivity_seconds": self.model_inactivity_seconds,
        }
