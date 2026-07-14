"""Desktop screen capture tool — 截取物理桌面屏幕"""

from __future__ import annotations

from src.tools.base import BaseTool, ToolSchema


class DesktopScreenshotTool(BaseTool):
    schema = ToolSchema(
        name="desktop_screenshot",
        description="截取当前物理桌面的屏幕截图，返回 base64 编码。用于分析桌面内容、窗口布局等。",
        parameters={
            "type": "object",
            "properties": {},
        },
    )

    async def execute(self) -> dict:
        from src.perception.screenshot import capture_screenshot

        result = await capture_screenshot()
        if result.get("success"):
            return {
                "success": True,
                "summary": f"Desktop screenshot captured ({result.get('size', '?')})",
                "screenshot_base64": result["base64"],
                "width": result["width"],
                "height": result["height"],
                "left": result["left"],
                "top": result["top"],
                "right": result["right"],
                "bottom": result["bottom"],
            }
        return {"success": False, "error": result.get("error", "Unknown screenshot error")}
