"""Desktop text input — clipboard + keystroke for typing into any application"""

from __future__ import annotations

import time

from src.tools.base import BaseTool, ToolSchema
from src.tools.desktop.native_control import activate_window


def _set_clipboard(text: str) -> None:
    """使用 Win32 Unicode 剪贴板写入文本，避免本地代码页造成中文乱码。"""
    import win32clipboard

    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _paste_via_ctrl_v() -> None:
    """通过 ctypes 模拟 Ctrl+V 粘贴"""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    KEYEVENTF_KEYUP = 0x0002
    VK_CONTROL = 0x11
    VK_V = 0x56

    # Ctrl down + V down
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    time.sleep(0.05)
    # V up + Ctrl up
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


class DesktopTypeTextTool(BaseTool):
    schema = ToolSchema(
        name="desktop_type_text",
        description=(
            "向当前聚焦的桌面应用程序输入文本。"
            "使用剪贴板粘贴方式，支持任意文本。"
            "输入前会按 app_name 重新聚焦并验证目标窗口。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要输入的文本内容",
                },
                "app_name": {
                    "type": "string",
                    "description": "必须接收文本的目标应用，如 wechat / 微信 / word",
                },
            },
            "required": ["text", "app_name"],
        },
    )

    async def execute(self, text: str, app_name: str) -> dict:
        try:
            hwnd, title = activate_window(app_name)
            _set_clipboard(text)
            time.sleep(0.1)
            # Clipboard operations or another UI may steal focus; verify again
            # immediately before the global Ctrl+V injection.
            hwnd, title = activate_window(app_name)
            _paste_via_ctrl_v()
            return {
                "success": True,
                "summary": f"Typed {len(text)} chars in verified window: {title}",
                "target_app": app_name,
                "window_title": title,
                "window_handle": hwnd,
                "foreground_verified": True,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}
