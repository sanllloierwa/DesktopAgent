"""LLM client factory — 统一 Anthropic / OpenAI / DeepSeek / Agnes / Kimi / Local 接口"""

from __future__ import annotations

from typing import Any

from src.utils.config import load_config, AppConfig, LLMConfig
from src.utils.secret import get_api_key

# Provider 默认 base URL
DEEPSEEK_BASE_URL = "https://api.deepseek.com"
AGNES_BASE_URL = "https://apihub.agnes-ai.com/v1"
KIMI_BASE_URL = "https://api.moonshot.cn/v1"
KIMI_GLOBAL_BASE_URL = "https://api.moonshot.ai/v1"

PROVIDER_BASE_URLS = {
    "deepseek": DEEPSEEK_BASE_URL,
    "agnes": AGNES_BASE_URL,
    "kimi": KIMI_BASE_URL,
}


class ContentBlock:
    """统一的消息内容块，屏蔽不同 provider 的响应格式差异"""
    def __init__(self, text: str) -> None:
        self.text = text


class LLMResponse:
    """统一的 LLM 响应，兼容 Anthropic 的 .content[0].text 访问模式"""
    def __init__(self, text: str, raw: Any = None) -> None:
        self.content = [ContentBlock(text)]
        self.raw = raw


class _OpenAICompatAdapter:
    """OpenAI 兼容接口适配（OpenAI / DeepSeek / Agnes / Kimi / Local）"""

    def __init__(self, client: Any, model: str, provider: str = "") -> None:
        self._client = client
        self.model = model
        self.provider = provider

    @property
    def messages(self) -> "_OpenAICompatAdapter":
        return self

    async def create(
        self,
        model: str = "",
        max_tokens: int = 4096,
        temperature: float = 0.3,
        system: str = "",
        messages: list[dict] | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        openai_msgs: list[dict] = []
        if system:
            openai_msgs.append({"role": "system", "content": system})
        for m in (messages or []):
            openai_msgs.append({"role": m["role"], "content": m["content"]})

        request: dict[str, Any] = {
            "model": model or self.model,
            "messages": openai_msgs,
        }
        selected_model = model or self.model
        if self.provider == "kimi":
            # Kimi models manage temperature themselves and reject unsupported
            # explicit values. K2.6 enables thinking by default; that reasoning
            # shares the completion budget with the final content. Planner calls
            # use JSON mode and do not need hidden reasoning, so disable it there.
            request["max_completion_tokens"] = max_tokens
            if json_mode:
                request["response_format"] = {"type": "json_object"}
                if selected_model.startswith(("kimi-k2.6", "kimi-k2.5")):
                    request["extra_body"] = {
                        "thinking": {"type": "disabled"},
                    }
        else:
            request["max_tokens"] = max_tokens
            request["temperature"] = temperature

        resp = await self._client.chat.completions.create(**request)
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
        json_mode: bool = False,
    ) -> Any:
        return self._client.messages.create(
            model=model or self.model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=messages or [],
        )


def _create_client(provider: str, model: str, base_url_override: str = "") -> Any:
    """内部工厂：根据 provider 创建适配后的客户端"""
    if provider == "anthropic":
        import anthropic
        api_key = get_api_key("anthropic")
        client = anthropic.Anthropic(api_key=api_key)
        return _AnthropicAdapter(client, model)

    elif provider == "deepseek":
        import openai
        api_key = get_api_key("deepseek")
        base_url = base_url_override or DEEPSEEK_BASE_URL
        client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        return _OpenAICompatAdapter(client, model, provider)

    elif provider == "openai":
        import openai
        api_key = get_api_key("openai")
        client = openai.AsyncOpenAI(api_key=api_key)
        return _OpenAICompatAdapter(client, model, provider)

    elif provider == "agnes":
        import openai
        api_key = get_api_key("agnes")
        base_url = base_url_override or AGNES_BASE_URL
        client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        return _OpenAICompatAdapter(client, model, provider)

    elif provider == "kimi":
        import openai
        api_key = get_api_key("kimi")
        base_url = base_url_override or KIMI_BASE_URL
        client = openai.AsyncOpenAI(base_url=base_url, api_key=api_key)
        return _OpenAICompatAdapter(client, model, provider)

    elif provider == "local":
        import openai
        from src.utils.secret import get_local_base_url
        base_url = base_url_override or get_local_base_url() or "http://localhost:11434/v1"
        client = openai.AsyncOpenAI(base_url=base_url, api_key="ollama")
        return _OpenAICompatAdapter(client, model, provider)

    else:
        raise ValueError(f"Unknown LLM provider: {provider}")


def create_llm_client(config: AppConfig | None = None, provider_override: str | None = None) -> Any:
    """创建 LLM 客户端实例，返回统一接口对象（有 .model 和 .messages.create()）

    配置优先级: 参数 provider_override > 用户设置(UI保存) > YAML 配置文件
    """
    if config is None:
        config = load_config()

    from src.utils.user_settings import get_user_settings
    us = get_user_settings()
    provider = provider_override or us.default_provider or config.llm.provider
    model = us.default_model or config.llm.model
    # A UI-selected provider must not inherit another provider's YAML endpoint.
    base_url = (
        getattr(config.llm, "base_url", "") or ""
        if provider == config.llm.provider
        else ""
    )

    return _create_client(provider, model, base_url)


def resolve_vision_target(config: AppConfig | None = None) -> tuple[str, str, str]:
    """Resolve effective vision settings, with UI values overriding YAML."""
    if config is None:
        config = load_config()

    from src.utils.user_settings import get_user_settings
    us = get_user_settings()
    provider = us.vision_provider or config.vision.provider
    model = us.vision_model or config.vision.model
    if provider == config.vision.provider:
        base_url = getattr(config.vision, "base_url", "") or ""
    else:
        base_url = PROVIDER_BASE_URLS.get(provider, "")
    return provider, model, base_url


def create_vision_client(config: AppConfig | None = None) -> Any:
    """创建视觉分析专用客户端，读取 config.vision 配置

    视觉模型与 UI 中选择的主模型相互独立。主模型可以是 DeepSeek，
    截图仍固定交给 vision.provider（默认 Agnes）处理。
    """
    provider, model, base_url = resolve_vision_target(config)

    return _create_client(provider, model, base_url)
