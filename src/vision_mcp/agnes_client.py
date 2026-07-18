"""Small multimodal MCP stdio client used by the main Agent process."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

from src.utils.config import AppConfig, load_config
from src.vision_mcp.artifacts import write_mcp_artifact


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
    call_arguments = dict(arguments or {})
    started = time.perf_counter()
    try:
        async with asyncio.timeout(config.vision.timeout_seconds):
            async with stdio_client(_server_parameters(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_name, call_arguments)
                    if getattr(result, "isError", False):
                        payload = _decode_result(result)
                        raise RuntimeError(payload.get("answer") or str(payload))
                    payload = _decode_result(result)
    except TimeoutError as exc:
        normalized = TimeoutError(
            f"Vision MCP timed out after {config.vision.timeout_seconds:g}s"
        )
        artifact_path = write_mcp_artifact(
            tool_name=tool_name,
            arguments=call_arguments,
            config=config,
            error=str(normalized),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        if artifact_path is not None:
            setattr(normalized, "mcp_artifact_path", str(artifact_path))
        raise normalized from exc
    except BaseExceptionGroup as exc:
        normalized = RuntimeError(exception_text(exc))
        artifact_path = write_mcp_artifact(
            tool_name=tool_name,
            arguments=call_arguments,
            config=config,
            error=str(normalized),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        if artifact_path is not None:
            setattr(normalized, "mcp_artifact_path", str(artifact_path))
        raise normalized from exc
    except Exception as exc:
        artifact_path = write_mcp_artifact(
            tool_name=tool_name,
            arguments=call_arguments,
            config=config,
            error=exception_text(exc),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        if artifact_path is not None:
            setattr(exc, "mcp_artifact_path", str(artifact_path))
        raise

    artifact_path = write_mcp_artifact(
        tool_name=tool_name,
        arguments=call_arguments,
        config=config,
        response=payload,
        duration_ms=(time.perf_counter() - started) * 1000,
    )
    if artifact_path is not None:
        payload["mcp_artifact_path"] = str(artifact_path)
    return payload


async def mcp_health(config: AppConfig | None = None) -> dict[str, Any]:
    return await call_agnes_tool("vision_health", config=config)


async def mcp_analyze_image(
    image_base64: str,
    question: str,
    media_type: str = "image/png",
    config: AppConfig | None = None,
) -> dict[str, Any]:
    return await call_agnes_tool(
        "analyze_image",
        {
            "image_base64": image_base64,
            "question": question,
            "media_type": media_type,
        },
        config=config,
    )
