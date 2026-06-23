"""Desktop application control — 启动/关闭/窗口管理"""

from __future__ import annotations

import subprocess
import time
from typing import Any

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.utils.config import load_config


class LaunchAppTool(BaseTool):
    schema = ToolSchema(
        name="launch_app",
        description="启动桌面应用程序",
        parameters={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用名称: wps | wechat | notepad | word",
                },
                "wait_time": {
                    "type": "number",
                    "description": "启动后等待秒数，默认 3",
                },
            },
            "required": ["app_name"],
        },
    )

    async def execute(self, app_name: str, wait_time: float = 3.0) -> dict:
        config = load_config()
        app_paths = config.desktop.app_paths

        path = app_paths.get(app_name, "")
        if not path and app_name == "word":
            path = self._find_word()

        if path:
            logger.info(f"Launching: {path}")
            subprocess.Popen([path], shell=True)
        elif app_name == "notepad":
            subprocess.Popen(["notepad.exe"])
        elif app_name == "wps":
            # 尝试常见路径
            for p in [
                r"C:\Program Files\Kingsoft\WPS Office\wps.exe",
                r"C:\Program Files (x86)\Kingsoft\WPS Office\wps.exe",
                r"C:\Program Files\Kingsoft\WPS Office\office6\wps.exe",
            ]:
                try:
                    subprocess.Popen([p])
                    break
                except Exception:
                    continue
            else:
                return {"success": False, "error": "Cannot find WPS executable", "window_handle": None}
        elif app_name == "wechat":
            for p in [
                r"C:\Program Files\Tencent\WeChat\WeChat.exe",
                r"C:\Program Files (x86)\Tencent\WeChat\WeChat.exe",
            ]:
                try:
                    subprocess.Popen([p])
                    break
                except Exception:
                    continue
            else:
                return {"success": False, "error": "Cannot find WeChat executable", "window_handle": None}
        else:
            return {"success": False, "error": f"Unknown app: {app_name}", "window_handle": None}

        time.sleep(wait_time)
        return {"success": True, "app_name": app_name, "summary": f"Launched {app_name}"}

    @staticmethod
    def _find_word() -> str | None:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for key_path in [
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Winword.exe",
                r"SOFTWARE\Microsoft\Office\ClickToRun\REGISTRY\MACHINE\Software\Microsoft\Office\16.0\Word\InstallRoot",
            ]:
                try:
                    with winreg.OpenKey(root, key_path) as key:
                        val, _ = winreg.QueryValueEx(key, "")
                        path = val if val.endswith(".exe") else val + r"\Winword.exe"
                        return path
                except OSError:
                    continue
        return None


class CloseAppTool(BaseTool):
    schema = ToolSchema(
        name="close_app",
        description="关闭桌面应用程序",
        parameters={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用名称: wps | wechat | notepad | word",
                },
            },
            "required": ["app_name"],
        },
    )

    async def execute(self, app_name: str) -> dict:
        import pygetwindow as gw
        try:
            for win in gw.getWindowsWithTitle(""):
                if app_name.lower() in win.title.lower():
                    win.close()
                    return {"success": True, "summary": f"Closed window: {win.title}"}
        except Exception:
            pass
        return {"success": False, "error": f"No window found for {app_name}"}
