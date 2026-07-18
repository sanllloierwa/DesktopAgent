"Config loader: YAML + env vars → Pydantic models"

from __future__ import annotations

import os
from pathlib import Path
from functools import lru_cache

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


# --- Pydantic models mirroring config/default.yaml ---

class AgentConfig(BaseModel):
    max_steps: int = 20
    step_timeout: int = 60
    retry_max: int = 3
    replan_max: int = 5
    screenshot_on_error: bool = True


class LLMConfig(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.3
    max_tokens: int = 4096


class VisionConfig(BaseModel):
    provider: str = "agnes"
    model: str = "agnes-2.0-flash"
    base_url: str = ""
    transport: str = "mcp"
    mcp_command: str = ""
    mcp_args: list[str] = Field(default_factory=lambda: ["-m", "src.vision_mcp.agnes_server"])
    timeout_seconds: float = 60.0
    artifact_output_enabled: bool = True
    artifact_dir: str = ""
    artifact_retention: int = 50


class ImageGenConfig(BaseModel):
    provider: str = "openai"
    model: str = "dall-e-3"
    default_size: str = "1024x1024"


class BrowserConfig(BaseModel):
    engine: str = "playwright"
    headless: bool = False
    viewport_width: int = 1280
    viewport_height: int = 720
    user_data_dir: str = "./browser_data"


class DesktopConfig(BaseModel):
    screenshot_dir: str = "./screenshots"
    app_paths: dict[str, str] = {}


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}:{function} | {message}"
    rotation: str = "10 MB"
    retention: str = "7 days"


class AppConfig(BaseModel):
    agent: AgentConfig = AgentConfig()
    llm: LLMConfig = LLMConfig()
    vision: VisionConfig = VisionConfig()
    image_gen: ImageGenConfig = ImageGenConfig()
    browser: BrowserConfig = BrowserConfig()
    desktop: DesktopConfig = DesktopConfig()
    logging: LoggingConfig = LoggingConfig()


# --- Settings from env vars ---

class Settings(BaseSettings):
    deepseek_api_key: str = ""
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    agnes_api_key: str = ""
    kimi_api_key: str = ""
    moonshot_api_key: str = ""
    local_llm_base_url: str = ""

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


# --- Loader ---

def _find_config_dir() -> Path:
    """从当前文件向上查找 config/ 目录"""
    candidates = [
        Path.cwd() / "config",
        Path(__file__).resolve().parent.parent.parent / "config",
    ]
    for p in candidates:
        if (p / "default.yaml").exists():
            return p
    raise FileNotFoundError("Cannot locate config/default.yaml")


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并两个 dict，override 的值优先"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


@lru_cache
def load_config(config_path: str | None = None) -> AppConfig:
    """加载配置：default.yaml + 可选的覆盖文件，Pydantic 校验"""
    config_dir = _find_config_dir()
    with open(config_dir / "default.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if config_path:
        with open(config_path, encoding="utf-8") as f:
            override = yaml.safe_load(f)
        data = _deep_merge(data, override)

    return AppConfig(**data)


@lru_cache
def load_settings() -> Settings:
    return Settings()
