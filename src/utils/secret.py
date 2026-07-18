"""Secret manager: read API keys from user settings > env vars.

加载优先级: 用户保存的 settings.json > 环境变量 > .env
"""

from __future__ import annotations

from src.utils.config import load_settings
from src.utils.user_settings import get_user_settings


def get_api_key(provider: str) -> str:
    # 1. 先查用户通过 UI 保存的 key
    us = get_user_settings()
    user_key = us.get_key(provider)
    if user_key:
        return user_key

    # 2. 再查环境变量 / .env
    settings = load_settings()
    key_map = {
        "deepseek": settings.deepseek_api_key or None,
        "anthropic": settings.anthropic_api_key or None,
        "openai": settings.openai_api_key or None,
        "agnes": settings.agnes_api_key or None,
        "kimi": settings.kimi_api_key or settings.moonshot_api_key or None,
    }
    key = key_map.get(provider)
    if key:
        return key

    raise ValueError(
        f"No API key found for '{provider}'. "
        f"Set it via the Web UI (Settings tab) or add to .env / environment."
    )


def get_local_base_url() -> str:
    settings = load_settings()
    return settings.local_llm_base_url
