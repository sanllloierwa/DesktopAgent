"""MCP stdio server exposing the configured multimodal model."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from src.vision_mcp.agnes_backend import analyze_image as run_vision_analysis, vision_info
from src.utils.secret import get_api_key


mcp = FastMCP("desktop-agent-multimodal-vision")


@mcp.tool()
def vision_health() -> dict[str, Any]:
    """Return vision configuration and API-key readiness without exposing secrets."""
    info = vision_info()
    try:
        get_api_key(str(info["provider"]))
        info["api_key_configured"] = True
    except ValueError:
        info["api_key_configured"] = False
    return info


@mcp.tool()
async def analyze_image(
    image_base64: str,
    question: str = "描述画面内容并列出可交互的 UI 元素",
    media_type: str = "image/png",
) -> dict[str, Any]:
    """Analyze a base64 image with the configured vision provider."""
    return await run_vision_analysis(image_base64, question, media_type)


@mcp.tool()
async def analyze_image_with_agnes(
    image_base64: str,
    question: str = "描述画面内容并列出可交互的 UI 元素",
    media_type: str = "image/png",
) -> dict[str, Any]:
    """Backward-compatible alias for older clients."""
    return await run_vision_analysis(image_base64, question, media_type)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
