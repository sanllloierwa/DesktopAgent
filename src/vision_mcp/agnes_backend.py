"""Configurable multimodal backend shared by MCP and direct fallback."""

from __future__ import annotations

import base64
import binascii
from typing import Any

from src.utils.config import AppConfig, load_config
from src.utils.llm_factory import (
    _AnthropicAdapter,
    create_vision_client,
    resolve_vision_target,
)


def vision_info(config: AppConfig | None = None) -> dict[str, Any]:
    config = config or load_config()
    provider, model, base_url = resolve_vision_target(config)
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "transport": config.vision.transport,
    }


async def analyze_image(
    image_base64: str,
    question: str,
    media_type: str = "image/png",
    config: AppConfig | None = None,
    json_mode: bool = False,
) -> dict[str, Any]:
    """Send one image to the configured vision model and normalize its response."""
    if not image_base64.strip():
        raise ValueError("image_base64 cannot be empty")
    if not media_type.startswith("image/"):
        raise ValueError(f"Unsupported media type: {media_type}")
    if len(image_base64) > 28_000_000:
        raise ValueError("Image is too large; maximum encoded size is 28 MB")
    try:
        base64.b64decode(image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("image_base64 is not valid base64 data") from exc

    config = config or load_config()
    llm = create_vision_client(config)
    if isinstance(llm, _AnthropicAdapter):
        response = await llm.messages.create(
            max_tokens=1024,
            temperature=0.2,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_base64,
                        },
                    },
                    {"type": "text", "text": question},
                ],
            }],
            json_mode=json_mode,
        )
    else:
        response = await llm.messages.create(
            max_tokens=1024,
            temperature=0.2,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{image_base64}",
                        },
                    },
                ],
            }],
            json_mode=json_mode,
        )

    answer = response.content[0].text if hasattr(response, "content") else str(response)
    return {**vision_info(config), "answer": answer}
