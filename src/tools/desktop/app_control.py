"""Desktop application control — 通用应用启动/关闭/窗口管理"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from loguru import logger

from src.tools.base import BaseTool, ToolSchema


def find_executable(app_name: str) -> str | None:
    """通用可执行文件查找，按优先级：

    1. shutil.which() — 系统 PATH
    2. Windows 注册表 App Paths — 已注册应用
    3. 常见安装目录扫描 — Program Files / LocalAppData

    返回可执行文件路径，找不到返回 None。
    """
    # 1) 直接给的就是完整路径
    if os.path.isfile(app_name) and app_name.lower().endswith(".exe"):
        return app_name

    # 2) shutil.which — 系统 PATH
    path = shutil.which(app_name)
    if path:
        return path

    # 不带 .exe 后缀试试
    if not app_name.lower().endswith(".exe"):
        path = shutil.which(app_name + ".exe")
        if path:
            return path

    # 3) 注册表 App Paths
    reg_path = _find_in_registry(app_name)
    if reg_path:
        return reg_path

    # 4) 常见安装目录搜索
    common_path = _search_common_dirs(app_name)
    if common_path:
        return common_path

    return None


def _find_in_registry(app_name: str) -> str | None:
    """在 Windows 注册表 App Paths 中查找可执行文件"""
    try:
        import winreg
    except ImportError:
        return None

    exe_name = app_name if app_name.lower().endswith(".exe") else app_name + ".exe"

    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for subkey in [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
            rf"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}",
        ]:
            try:
                with winreg.OpenKey(root, subkey) as key:
                    val, _ = winreg.QueryValueEx(key, "")
                    if val and os.path.isfile(val):
                        return val
            except OSError:
                continue
    return None


def _search_common_dirs(app_name: str) -> str | None:
    """在常见安装目录中搜索可执行文件"""
    exe_name = app_name if app_name.lower().endswith(".exe") else app_name + ".exe"

    search_roots = []
    # Program Files variants
    for pf in [os.environ.get("ProgramFiles", "C:\\Program Files"),
               os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")]:
        if pf:
            search_roots.append(Path(pf))
    # LocalAppData
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        search_roots.append(Path(local_appdata))

    for root in search_roots:
        if not root.exists():
            continue
        # 直接子目录
        for candidate in [
            root / exe_name,
            root / app_name / exe_name,
        ]:
            if candidate.is_file():
                return str(candidate)
        # 浅层搜索（最多 2 层深度，避免全盘扫描）
        try:
            for item in root.iterdir():
                if not item.is_dir():
                    continue
                target = item / exe_name
                if target.is_file():
                    return str(target)
                # 再深一层
                try:
                    for sub_item in item.iterdir():
                        if sub_item.is_dir():
                            target2 = sub_item / exe_name
                            if target2.is_file():
                                return str(target2)
                except PermissionError:
                    continue
        except PermissionError:
            continue

    return None


class LaunchAppTool(BaseTool):
    """通用应用启动工具。

    接受任意应用名称或可执行文件路径，按 find_executable() 的策略查找并启动。
    可通过传入 discovered_apps 将动态扫描结果注入工具描述中供 Planner 参考。
    """

    def __init__(self, discovered_apps: dict[str, str] | None = None) -> None:
        self.discovered_apps = discovered_apps or {}
        self.schema = self._build_schema()

    def _build_schema(self) -> ToolSchema:
        """根据已发现的应用动态生成 schema 描述"""
        desc = (
            "启动桌面应用程序。接受应用名称（如 chrome、firefox、vscode、wps、wechat、notepad、word）"
            "或完整可执行文件路径。"
        )
        if self.discovered_apps:
            app_list = ", ".join(sorted(self.discovered_apps.keys()))
            desc += f" 当前系统已发现: {app_list}。"

        return ToolSchema(
            name="launch_app",
            description=desc,
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "应用名称或可执行文件路径（如 chrome / firefox / notepad），名称不区分大小写",
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
        # 先查动态发现列表
        if app_name.lower() in {k.lower() for k in self.discovered_apps}:
            for k, v in self.discovered_apps.items():
                if k.lower() == app_name.lower():
                    path = v
                    break
            else:
                path = ""
        else:
            path = find_executable(app_name)

        if path:
            logger.info(f"Launching: {path}")
            try:
                subprocess.Popen([path], shell=True)
            except Exception as exc:
                return {"success": False, "error": f"Launch failed: {exc}", "summary": ""}
            time.sleep(wait_time)
            return {"success": True, "app_name": app_name, "path": path, "summary": f"Launched {app_name} ({path})"}

        return {
            "success": False,
            "error": f"未找到应用 '{app_name}'。请确认应用已安装，或提供完整路径。",
            "summary": "",
        }


class CloseAppTool(BaseTool):

    def __init__(self, discovered_apps: dict[str, str] | None = None) -> None:
        self._discovered = discovered_apps or {}
        self.schema = self._build_schema()

    def _build_schema(self) -> ToolSchema:
        desc = "关闭桌面应用程序窗口（按窗口标题匹配）"
        return ToolSchema(
            name="close_app",
            description=desc,
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "应用名称，用于匹配窗口标题（如 chrome / wps / wechat），不区分大小写",
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
        except Exception as exc:
            return {"success": False, "error": str(exc), "summary": ""}
        return {"success": False, "error": f"No window found for {app_name}", "summary": ""}
