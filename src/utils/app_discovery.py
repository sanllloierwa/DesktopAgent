"""动态应用发现 — 扫描系统已安装应用，供 LaunchAppTool 使用"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from loguru import logger

# 值得暴露给 Planner 的常用应用（即使不在 Start Menu 中也会检查 PATH）
_COMMON_APPS = [
    "chrome", "firefox", "msedge", "opera", "brave",
    "code",    # VS Code
    "notepad", "winword", "wps",
    "spotify", "vlc",
    "obsidian", "notion",
    "slack", "discord", "telegram",
    "postman", "dbeaver",
]


def discover_apps() -> dict[str, str]:
    """扫描系统并返回 {显示名称: exe路径} 的已安装应用字典。

    扫描来源：
    1. Start Menu .lnk 快捷方式（用户 + 所有用户）
    2. 常用应用 PATH 检查
    3. 注册表 Uninstall 条目（补充信息）
    """
    discovered: dict[str, str] = {}

    # 1) Start Menu .lnk 文件
    _scan_start_menu(discovered)

    # 2) 常用应用 PATH 检查（不在 Start Menu 但已安装的）
    _scan_common_apps(discovered)

    # 3) 注册表补充
    _scan_registry_uninstall(discovered)

    # 清洗：只保留 exe 存在的条目，名称去重优先短名
    cleaned: dict[str, str] = {}
    for name, path in discovered.items():
        if not os.path.isfile(path):
            continue
        # 归一化名称：去公司后缀、去版本号
        clean_name = _clean_app_name(name)
        if clean_name not in cleaned or len(clean_name) < len(name):
            cleaned[clean_name] = path

    logger.info(f"App discovery found {len(cleaned)} applications")
    return cleaned


def _scan_start_menu(discovered: dict[str, str]) -> None:
    """从开始菜单 .lnk 文件提取应用名和路径"""
    start_menu_dirs = []
    for env_var in ["APPDATA", "PROGRAMDATA"]:
        base = os.environ.get(env_var, "")
        if base:
            start_menu_dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")

    for sm_dir in start_menu_dirs:
        if not sm_dir.exists():
            continue
        for lnk_path in sm_dir.rglob("*.lnk"):
            try:
                info = _parse_lnk(lnk_path)
                if info:
                    name, target = info
                    # 跳过卸载程序、帮助文件
                    if any(skip in name.lower() for skip in ["uninstall", "unins", "help", "readme", "license"]):
                        continue
                    if name.lower() not in {k.lower() for k in discovered}:
                        discovered[name] = target
            except Exception:
                continue


def _parse_lnk(lnk_path: Path) -> tuple[str, str] | None:
    """解析 Windows .lnk 快捷方式，返回 (名称, 目标路径)"""
    try:
        import win32com.client
    except ImportError:
        return None

    try:
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut = shell.CreateShortcut(str(lnk_path))
        name = lnk_path.stem
        target = shortcut.TargetPath
        if target and os.path.isfile(target) and target.lower().endswith(".exe"):
            return name, target
    except Exception:
        pass
    return None


def _scan_common_apps(discovered: dict[str, str]) -> None:
    """检查常用应用是否在 PATH 中"""
    existing_names = {k.lower() for k in discovered}
    for app in _COMMON_APPS:
        if app.lower() in existing_names:
            continue
        path = shutil.which(app) or shutil.which(app + ".exe")
        if path:
            discovered[app] = path


def _scan_registry_uninstall(discovered: dict[str, str]) -> None:
    """从注册表 Uninstall 条目补充应用信息"""
    try:
        import winreg
    except ImportError:
        return

    existing_names = {k.lower() for k in discovered}
    uninstall_keys = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for root, subkey in uninstall_keys:
        try:
            with winreg.OpenKey(root, subkey) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, subkey_name) as app_key:
                            display_name = _read_reg(app_key, "DisplayName")
                            install_path = _read_reg(app_key, "InstallLocation")
                            if display_name and install_path:
                                name_lower = display_name.lower()
                                if name_lower not in existing_names and not any(
                                    s in name_lower for s in ["driver", "runtime", "sdk", "update", "plugin"]
                                ):
                                    # 尝试在 InstallLocation 下找同名 exe
                                    loc = Path(install_path)
                                    if loc.exists():
                                        for exe_name in [
                                            display_name + ".exe",
                                            _clean_app_name(display_name) + ".exe",
                                        ]:
                                            candidate = loc / exe_name
                                            if candidate.is_file():
                                                discovered[display_name] = str(candidate)
                                                existing_names.add(name_lower)
                                                break
                    except (OSError, FileNotFoundError):
                        continue
        except OSError:
            continue


def _read_reg(key: Any, value_name: str) -> str | None:
    """安全读取注册表字符串值"""
    try:
        import winreg
        val, _ = winreg.QueryValueEx(key, value_name)
        return str(val).strip() if val else None
    except Exception:
        return None


def _clean_app_name(name: str) -> str:
    """清洗应用名称：去公司后缀、版本号、多余空格"""
    name = name.strip()
    # 去掉常见公司后缀
    for suffix in [" Inc.", " Inc", " LLC", " Ltd.", " Ltd", " Corp.", " Corp",
                   " (x64)", " (x86)", " (64-bit)", " (32-bit)"]:
        if name.endswith(suffix):
            name = name[:-len(suffix)].rstrip()
    # 去掉版本号尾缀 " 2.1.3"
    parts = name.rsplit(" ", 1)
    if len(parts) == 2 and all(c.isdigit() or c == "." for c in parts[1]):
        name = parts[0]
    return name
