from __future__ import annotations

from src.tools.desktop.app_control import LaunchAppTool
from src.ui.events import task_done_console_label


def test_wechat_english_alias_matches_discovered_chinese_app() -> None:
    tool = LaunchAppTool(discovered_apps={"微信": r"C:\Apps\WeChat.exe"})

    assert tool._lookup_app("wechat") == r"C:\Apps\WeChat.exe"
    assert tool._lookup_app("weixin") == r"C:\Apps\WeChat.exe"


def test_task_done_console_label_distinguishes_failure() -> None:
    assert task_done_console_label({"success": True}) == "DONE"
    assert task_done_console_label({"success": False}) == "FAILED"
    assert task_done_console_label({}) == "FAILED"
