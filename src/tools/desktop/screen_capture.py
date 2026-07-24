"""Desktop screen capture tool — 截取物理桌面屏幕"""

from __future__ import annotations

from src.tools.base import BaseTool, ToolSchema


class DesktopScreenshotTool(BaseTool):
    schema = ToolSchema(
        name="desktop_screenshot",
        description=(
            "截取物理桌面或指定应用窗口，返回 base64 编码。"
            "操作微信时必须设置 app_name='wechat'，工具会先验证前台窗口并只截取微信。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "可选目标应用；微信统一使用 wechat",
                },
            },
        },
    )

    async def execute(self, app_name: str = "") -> dict:
        from src.perception.screenshot import capture_screenshot

        window_title = ""
        window_handle: int | None = None
        region = None
        if app_name:
            try:
                import win32gui

                from src.tools.desktop.native_control import activate_window

                window_handle, window_title = activate_window(app_name)
                left, top, right, bottom = win32gui.GetWindowRect(window_handle)
                if right <= left or bottom <= top:
                    return {
                        "success": False,
                        "error": f"Invalid target window bounds: {(left, top, right, bottom)}",
                    }
                region = (left, top, right, bottom)
            except Exception as exc:
                return {
                    "success": False,
                    "error": f"Target window screenshot unavailable: {exc}",
                }

        result = (
            await capture_screenshot(region=region)
            if region is not None
            else await capture_screenshot()
        )
        if result.get("success"):
            return {
                "success": True,
                "summary": (
                    f"Screenshot captured for {window_title or 'desktop'} "
                    f"({result.get('size', '?')})"
                ),
                "screenshot_base64": result["base64"],
                "width": result["width"],
                "height": result["height"],
                "left": result["left"],
                "top": result["top"],
                "right": result["right"],
                "bottom": result["bottom"],
                "target_app": app_name or None,
                "window_title": window_title or None,
                "window_handle": window_handle,
                "foreground_verified": bool(app_name),
            }
        return {"success": False, "error": result.get("error", "Unknown screenshot error")}
