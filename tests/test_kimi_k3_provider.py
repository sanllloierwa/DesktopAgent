from __future__ import annotations

import base64
from types import SimpleNamespace

from src.utils.config import AppConfig, Settings
from src.utils.llm_factory import KIMI_BASE_URL, _OpenAICompatAdapter, _create_client
from src.vision_mcp.agnes_backend import analyze_image
from src.vision_mcp.agnes_client import mcp_analyze_image


class _FakeCompletions:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        message = SimpleNamespace(content="Kimi response")
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _FakeOpenAIClient:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


async def test_kimi_adapter_uses_k3_compatible_parameters() -> None:
    raw_client = _FakeOpenAIClient()
    adapter = _OpenAICompatAdapter(raw_client, "kimi-k3", provider="kimi")

    response = await adapter.messages.create(
        max_tokens=2048,
        temperature=0.2,
        messages=[{"role": "user", "content": "写一段文本"}],
    )

    assert response.content[0].text == "Kimi response"
    request = raw_client.completions.requests[0]
    assert request["model"] == "kimi-k3"
    assert request["max_completion_tokens"] == 2048
    assert "max_tokens" not in request
    assert "temperature" not in request


def test_kimi_factory_uses_official_endpoint(monkeypatch) -> None:
    calls = []

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs) -> None:
            calls.append(kwargs)

    monkeypatch.setattr("openai.AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr("src.utils.llm_factory.get_api_key", lambda provider: "kimi-key")

    adapter = _create_client("kimi", "kimi-k3")

    assert isinstance(adapter, _OpenAICompatAdapter)
    assert adapter.provider == "kimi"
    assert calls == [{"base_url": KIMI_BASE_URL, "api_key": "kimi-key"}]


async def test_kimi_multimodal_request_uses_image_url(monkeypatch) -> None:
    raw_client = _FakeOpenAIClient()
    adapter = _OpenAICompatAdapter(raw_client, "kimi-k3", provider="kimi")
    config = AppConfig.model_validate({
        "vision": {
            "provider": "kimi",
            "model": "kimi-k3",
            "base_url": KIMI_BASE_URL,
            "transport": "direct",
        },
    })
    monkeypatch.setattr(
        "src.vision_mcp.agnes_backend.create_vision_client",
        lambda _config: adapter,
    )
    image_base64 = base64.b64encode(b"image-bytes").decode("ascii")

    result = await analyze_image(image_base64, "描述截图", config=config)

    assert result["provider"] == "kimi"
    assert result["model"] == "kimi-k3"
    content = raw_client.completions.requests[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "描述截图"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


async def test_mcp_client_uses_generic_multimodal_tool(monkeypatch) -> None:
    calls = []

    async def fake_call(tool_name, arguments, config=None):
        calls.append((tool_name, arguments))
        return {"answer": "ok"}

    monkeypatch.setattr("src.vision_mcp.agnes_client.call_agnes_tool", fake_call)

    await mcp_analyze_image("aW1hZ2U=", "描述")

    assert calls[0][0] == "analyze_image"
    assert calls[0][1]["media_type"] == "image/png"


def test_kimi_settings_and_ui_options_are_registered() -> None:
    from src.ui.ctk_app import PROVIDER_INFO as CTK_PROVIDERS
    from src.ui.gradio_app import PROVIDER_INFO as GRADIO_PROVIDERS

    settings = Settings(kimi_api_key="kimi", moonshot_api_key="moonshot")
    assert settings.kimi_api_key == "kimi"
    assert settings.moonshot_api_key == "moonshot"
    assert CTK_PROVIDERS["kimi"]["models"] == ["kimi-k3"]
    assert GRADIO_PROVIDERS["kimi"]["base_url"] == KIMI_BASE_URL
