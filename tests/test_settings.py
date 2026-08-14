from pathlib import Path

from cortex_v5.settings import Settings


def test_settings_resolve_relative_paths_inside_v5_root(tmp_path: Path):
    settings = Settings.from_env(
        {
            "CORTEX_V5_DATA_DIR": "state",
            "CORTEX_V5_ALLOWED_ROOT": "work",
            "LITELLM_URL": "https://example.invalid/v1/",
            "LITELLM_MASTER_KEY": "secret-value",
        },
        project_root=tmp_path,
    )

    assert settings.data_dir == (tmp_path / "state").resolve()
    assert settings.allowed_root == (tmp_path / "work").resolve()
    assert settings.litellm_url == "https://example.invalid/v1"
    assert settings.litellm_api_key == "secret-value"


def test_public_settings_never_include_secret_values(tmp_path: Path):
    settings = Settings.from_env(
        {
            "LITELLM_URL": "https://example.invalid",
            "LITELLM_MASTER_KEY": "do-not-print",
            "CORTEX_V5_HTTP_BEARER": "also-secret",
        },
        project_root=tmp_path,
    )

    rendered = repr(settings.public())
    assert "do-not-print" not in rendered
    assert "also-secret" not in rendered
    assert settings.public()["litellm_configured"] is True
    assert settings.public()["http_auth_enabled"] is True
