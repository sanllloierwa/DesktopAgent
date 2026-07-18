"""Debug CLI for the Agnes MCP vision service."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import mimetypes
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.vision_mcp.agnes_client import exception_text, mcp_analyze_image, mcp_health
from src.perception.screenshot import capture_screenshot


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Debug Agnes vision over MCP")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health", help="Check MCP startup, model configuration and API key")

    image = sub.add_parser("image", help="Analyze a local image")
    image.add_argument("path", type=Path)
    image.add_argument("--question", "-q", default="描述图片内容和可交互元素")

    screen = sub.add_parser("screen", help="Capture and analyze the primary monitor")
    screen.add_argument("--question", "-q", default="描述当前桌面和可交互元素")
    return parser


async def _run(args: argparse.Namespace) -> dict:
    if args.command == "health":
        return await mcp_health()
    if args.command == "image":
        data = base64.b64encode(args.path.read_bytes()).decode("ascii")
        media_type = mimetypes.guess_type(args.path.name)[0] or "image/png"
        return await mcp_analyze_image(data, args.question, media_type)

    screenshot = await capture_screenshot()
    if not screenshot.get("success"):
        raise RuntimeError(screenshot.get("error", "Screenshot failed"))
    return await mcp_analyze_image(screenshot["base64"], args.question)


def main() -> None:
    args = _parser().parse_args()
    try:
        result = asyncio.run(_run(args))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as exc:
        payload = {"success": False, "error": exception_text(exc)}
        artifact_path = getattr(exc, "mcp_artifact_path", None)
        if artifact_path:
            payload["mcp_artifact_path"] = str(artifact_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
