from __future__ import annotations

from src.utils.config import AppConfig
from src.utils.llm_factory import KIMI_BASE_URL, resolve_vision_target
from src.utils.user_settings import UserSettings
from src.vision_mcp.agnes_backend import vision_info


def test_ui_vision_selection_overrides_yaml(monkeypatch) -> None:
    config = AppConfig.model_validate({
        "vision": {
            "provider": "agnes",
            "model": "agnes-2.0-flash",
            "base_url": "https://apihub.agnes-ai.com/v1",
        },
    })
    settings = UserSettings(vision_provider="kimi", vision_model="kimi-k3")
    monkeypatch.setattr(
        "src.utils.user_settings.get_user_settings",
        lambda: settings,
    )

    assert resolve_vision_target(config) == ("kimi", "kimi-k3", KIMI_BASE_URL)
    assert vision_info(config) == {
        "provider": "kimi",
        "model": "kimi-k3",
        "base_url": KIMI_BASE_URL,
        "transport": "mcp",
    }


def test_empty_ui_vision_selection_falls_back_to_yaml(monkeypatch) -> None:
    config = AppConfig.model_validate({
        "vision": {
            "provider": "agnes",
            "model": "agnes-2.0-flash",
            "base_url": "https://custom-agnes.example/v1",
        },
    })
    monkeypatch.setattr(
        "src.utils.user_settings.get_user_settings",
        lambda: UserSettings(),
    )

    assert resolve_vision_target(config) == (
        "agnes",
        "agnes-2.0-flash",
        "https://custom-agnes.example/v1",
    )


def test_vision_selection_is_persisted(monkeypatch, tmp_path) -> None:
    import src.utils.user_settings as settings_module

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "DEFAULT_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_module, "_user_settings", None)

    settings_module.save_user_settings(UserSettings(
        vision_provider="kimi",
        vision_model="kimi-k3",
    ))
    monkeypatch.setattr(settings_module, "_user_settings", None)
    loaded = settings_module.load_user_settings()

    assert loaded.vision_provider == "kimi"
    assert loaded.vision_model == "kimi-k3"


def test_dropdown_selection_persists_without_rewriting_keys(monkeypatch, tmp_path) -> None:
    import json
    import src.utils.user_settings as settings_module

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"kimi_api_key": "secret", "default_provider": "deepseek"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings_module, "DEFAULT_DIR", tmp_path)
    monkeypatch.setattr(settings_module, "SETTINGS_FILE", settings_file)
    monkeypatch.setattr(settings_module, "_user_settings", None)

    settings_module.save_model_selection(
        default_provider="kimi",
        default_model="kimi-k3",
        vision_provider="kimi",
        vision_model="kimi-k3",
    )

    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["kimi_api_key"] == "secret"
    assert saved["default_provider"] == "kimi"
    assert saved["vision_provider"] == "kimi"
