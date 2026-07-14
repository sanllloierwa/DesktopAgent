"""MCP stdio server exposing Agnes as a vision capability."""

from typing import Any

from mcp.server.fastmcp import FastMCP

from src.vision_mcp.agnes_backend import analyze_image, vision_info
from src.utils.secret import get_api_key


mcp = FastMCP("desktop-agent-agnes-vision")


@mcp.tool()
def vision_health() -> dict[str, Any]:
    """Return Agnes vision configuration and API-key readiness without exposing secrets."""
    info = vision_info()
    try:
        get_api_key(str(info["provider"]))
        info["api_key_configured"] = True
    except ValueError:
        info["api_key_configured"] = False
    return info


@mcp.tool()
async def analyze_image_with_agnes(
    image_base64: str,
    question: str = "描述画面内容并列出可交互的 UI 元素",
    media_type: str = "image/png",
) -> dict[str, Any]:
    """Analyze a base64 image with Agnes and return a textual answer for DeepSeek."""
    return await analyze_image(image_base64, question, media_type)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
