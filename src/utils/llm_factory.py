"""LLM client factory — 统一 Anthropic / OpenAI / DeepSeek 接口"""

from __future__ import annotations

from typing import Any

from src.utils.config import load_config, AppConfig, LLMConfig
from src.utils.secret import get_api_key

# DeepSeek API 默认地址
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


class ContentBlock:
    """统一的消息内容块，屏蔽不同 provider 的响应格式差异"""
    def __init__(self, text: str) -> None:
        self.text = text


class LLMResponse:
    """统一的 LLM 响应，兼容 Anthropic 的 .content[0].text 访问模式"""
    def __init__(self, text: str, raw: Any = None) -> None:
        self.content = [ContentBlock(text)]
        self.raw = raw


class _DeepSeekAdapter:
    """DeepSeek / OpenAI 兼容 → Anthropic 风格接口适配"""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model

    @property
    def messages(self) -> "_DeepSeekAdapter":
        return self

    async def create(
        self,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        system: str = "",
        messages: list[dict] | None = None,
    ) -> LLMResponse:
        openai_msgs: list[dict] = []
        if system:
            openai_msgs.append({"role": "system", "content": system})
        for m in (messages or []):
            openai_msgs.append({"role": m["role"], "content": m["content"]})

        resp = await self._client.chat.completions.create(
            model=model or self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=openai_msgs,
        )
        text = resp.choices[0].message.content or ""
        return LLMResponse(text, raw=resp)


class _AnthropicAdapter:
    """Anthropic 原生接口适配"""

    def __init__(self, client: Any, model: str) -> None:
        self._client = client
        self.model = model

    @property
    def messages(self) -> "_AnthropicAdapter":
        return self

    async def create(
        self,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        system: str = "",
        messages: list[dict] | None = None,
    ) -> Any:
        return self._client.messages.create(
            model=model or self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages or [],
        )


def create_llm_client(config: AppConfig | None = None, provider_override: str | None = None) -> Any:
    """创建 LLM 客户端实例，返回统一接口对象（有 .model 和 .messages.create()）

    配置优先级: 参数 provider_override > 用户设置(UI保存) > YAML 配置文件
    """
    if config is None:
        config = load_config()

    # 用户通过 UI 保存的设置优先于 YAML
    from src.utils.user_settings import get_user_settings
    us = get_user_settings()
    provider = provider_override or us.default_provider or config.llm.provider
    model = us.default_model or config.llm.model

    llm_cfg: LLMConfig = config.llm

    if provider == "anthropic":
        import anthropic
        api_key = get_api_key("anthropic")
        client = anthropic.Anthropic(api_key=api_key)
        return _AnthropicAdapter(client, model)

    elif provider == "deepseek":
        import openai
        api_key = get_api_key("deepseek")
        base_url = getattr(llm_cfg, "base_url", None) or DEEPSEEK_BASE_URL
        client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        return _DeepSeekAdapter(client, model)

    elif provider == "openai":
        import openai
        api_key = get_api_key("openai")
        client = openai.AsyncOpenAI(api_key=api_key)
        return _DeepSeekAdapter(client, model)

    elif provider == "local":
        import openai
        from src.utils.secret import get_local_base_url
        base_url = get_local_base_url() or "http://localhost:11434/v1"
        client = openai.AsyncOpenAI(base_url=base_url, api_key="ollama")
        return _DeepSeekAdapter(client, model)

    else:
        raise ValueError(f"Unknown LLM provider: {provider}")
