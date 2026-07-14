"""Small MCP stdio client used by the main Agent process."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from src.utils.config import AppConfig, load_config


def exception_text(exc: BaseException) -> str:
    """Flatten nested ExceptionGroup errors produced by MCP/AnyIO cleanup."""
    if isinstance(exc, BaseExceptionGroup):
        parts = [exception_text(item) for item in exc.exceptions]
        return "; ".join(part for part in parts if part) or str(exc)
    return str(exc)


def _server_parameters(config: AppConfig) -> Any:
    from mcp import StdioServerParameters

    return StdioServerParameters(
        command=config.vision.mcp_command or sys.executable,
        args=config.vision.mcp_args,
    )


def _decode_result(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured

    texts = [block.text for block in getattr(result, "content", []) if hasattr(block, "text")]
    raw = "\n".join(texts).strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"answer": value}
    except json.JSONDecodeError:
        return {"answer": raw}


async def call_agnes_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    config: AppConfig | None = None,
) -> dict[str, Any]:
    """Start the configured MCP server, call one tool, and return its JSON payload."""
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    config = config or load_config()
    try:
        async with asyncio.timeout(config.vision.timeout_seconds):
            async with stdio_client(_server_parameters(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, arguments or {})
                    if getattr(result, "isError", False):
                        payload = _decode_result(result)
                        raise RuntimeError(payload.get("answer") or str(payload))
                    return _decode_result(result)
    except TimeoutError as exc:
        raise TimeoutError(
            f"Vision MCP timed out after {config.vision.timeout_seconds:g}s"
        ) from exc
    except BaseExceptionGroup as exc:
        raise RuntimeError(exception_text(exc)) from exc


async def mcp_health(config: AppConfig | None = None) -> dict[str, Any]:
    return await call_agnes_tool("vision_health", config=config)


async def mcp_analyze_image(
    image_base64: str,
    question: str,
    media_type: str = "image/png",
    config: AppConfig | None = None,
) -> dict[str, Any]:
    return await call_agnes_tool(
        "analyze_image_with_agnes",
        {
            "image_base64": image_base64,
            "question": question,
            "media_type": media_type,
        },
        config=config,
    )
