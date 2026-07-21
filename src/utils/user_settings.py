"""User settings persistence — API keys saved via UI, stored in JSON.

加载优先级: 环境变量 > 用户保存的 settings.json > .env 文件
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock
from dataclasses import dataclass, field

DEFAULT_DIR = Path.home() / ".desktop-agent"
SETTINGS_FILE = DEFAULT_DIR / "settings.json"


@dataclass
class UserSettings:
    deepseek_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    agnes_api_key: str = ""
    kimi_api_key: str = ""
    default_provider: str = "deepseek"
    default_model: str = "deepseek-chat"
    vision_provider: str = ""
    vision_model: str = ""
    last_used_provider: str = ""

    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def has_key(self, provider: str) -> bool:
        key = getattr(self, f"{provider}_api_key", "")
        return bool(key)

    def get_key(self, provider: str) -> str:
        return getattr(self, f"{provider}_api_key", "")

    def set_key(self, provider: str, key: str) -> None:
        if hasattr(self, f"{provider}_api_key"):
            setattr(self, f"{provider}_api_key", key)

    def mask_key(self, provider: str) -> str:
        """返回脱敏后的 key 用于 UI 展示"""
        key = self.get_key(provider)
        if not key:
            return ""
        if len(key) <= 8:
            return "*" * len(key)
        return key[:4] + "*" * (len(key) - 8) + key[-4:]


# 全局单例
_user_settings: UserSettings | None = None


def _ensure_dir() -> None:
    DEFAULT_DIR.mkdir(parents=True, exist_ok=True)


def load_user_settings() -> UserSettings:
    """加载用户设置，优先环境变量，其次 settings.json"""
    global _user_settings

    us = UserSettings()
    _ensure_dir()

    # 从 settings.json 加载
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key, val in data.items():
                if hasattr(us, key):
                    setattr(us, key, val)
        except (json.JSONDecodeError, OSError):
            pass

    # 环境变量覆盖文件中的值
    env_map = {
        "deepseek_api_key": "DEEPSEEK_API_KEY",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
        "agnes_api_key": "AGNES_API_KEY",
        "kimi_api_key": "KIMI_API_KEY",
    }
    for attr, env_var in env_map.items():
        env_val = os.environ.get(env_var, "")
        if env_val:
            setattr(us, attr, env_val)

    _user_settings = us
    return us


def save_user_settings(settings: UserSettings) -> None:
    """持久化用户设置到 JSON 文件"""
    global _user_settings
    _ensure_dir()
    data = {
        "deepseek_api_key": settings.deepseek_api_key,
        "anthropic_api_key": settings.anthropic_api_key,
        "openai_api_key": settings.openai_api_key,
        "agnes_api_key": settings.agnes_api_key,
        "kimi_api_key": settings.kimi_api_key,
        "default_provider": settings.default_provider,
        "default_model": settings.default_model,
        "vision_provider": settings.vision_provider,
        "vision_model": settings.vision_model,
        "last_used_provider": settings.last_used_provider,
    }
    with settings._lock:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    _user_settings = settings


def save_model_selection(
    *,
    default_provider: str | None = None,
    default_model: str | None = None,
    vision_provider: str | None = None,
    vision_model: str | None = None,
) -> UserSettings:
    """Persist model dropdown changes without rewriting API keys."""
    global _user_settings
    settings = get_user_settings()
    updates = {
        key: value
        for key, value in {
            "default_provider": default_provider,
            "default_model": default_model,
            "vision_provider": vision_provider,
            "vision_model": vision_model,
        }.items()
        if value is not None
    }
    for key, value in updates.items():
        setattr(settings, key, value)

    _ensure_dir()
    data: dict = {}
    if SETTINGS_FILE.exists():
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            pass
    data.update(updates)
    with settings._lock:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    _user_settings = settings
    return settings


def get_user_settings() -> UserSettings:
    global _user_settings
    if _user_settings is None:
        _user_settings = load_user_settings()
    return _user_settings
