"""Desktop text input — clipboard + keystroke for typing into any application"""

from __future__ import annotations

import subprocess
import time
from typing import Any

from src.tools.base import BaseTool, ToolSchema


def _set_clipboard(text: str) -> None:
    """将文本放入 Windows 剪贴板（通过 clip.exe）"""
    subprocess.run(
        ["clip.exe"],
        input=text,
        encoding="utf-8",
        shell=True,
        check=False,
    )


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
            "输入前请确保目标应用窗口已打开并获得焦点。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要输入的文本内容",
                },
            },
            "required": ["text"],
        },
    )

    async def execute(self, text: str) -> dict:
        try:
            _set_clipboard(text)
            time.sleep(0.1)
            _paste_via_ctrl_v()
            return {"success": True, "summary": f"Typed {len(text)} chars via clipboard paste"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}
