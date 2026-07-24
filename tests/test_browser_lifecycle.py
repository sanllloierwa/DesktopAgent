from __future__ import annotations

import uuid

import pytest

from src.agent.planner import Planner
from src.agent.run_lock import TaskRunLock
from src.tools.base import ToolRegistry
from src.tools.browser.navigate import _select_reusable_page
from src.tools.desktop import app_control
from src.tools.desktop.app_control import LaunchAppTool
from src.ui.ctk_app import AgentThread
from src.ui.gradio_app import UIBridge


class FakePage:
    def __init__(self, url: str, closed: bool = False) -> None:
        self.url = url
        self.closed = closed

    def is_closed(self) -> bool:
        return self.closed


def test_page_reuse_prefers_existing_target_origin() -> None:
    new_tab = FakePage("chrome://newtab/")
    zhihu = FakePage("https://www.zhihu.com/")
    other = FakePage("https://example.com/")

    selected = _select_reusable_page(
        [new_tab, other, zhihu],
        "https://www.zhihu.com/write",
    )

    assert selected is zhihu


def test_page_reuse_uses_new_tab_before_creating_another_page() -> None:
    new_tab = FakePage("chrome://newtab/")
    other = FakePage("https://example.com/")

    assert _select_reusable_page([other, new_tab], "") is new_tab


@pytest.mark.asyncio
async def test_launch_browser_reuses_existing_cdp_instance(monkeypatch) -> None:
    monkeypatch.setattr(app_control, "_cdp_browser_available", lambda: True)
    monkeypatch.setattr(
        app_control.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("must not start a second browser"),
    )
    tool = LaunchAppTool(discovered_apps={
        "chrome": r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    })

    result = await tool.execute("chrome", wait_time=0)

    assert result["success"] is True
    assert result["already_running"] is True


def test_ui_bridges_reject_duplicate_queued_tasks() -> None:
    registry = ToolRegistry()
    for bridge in (UIBridge(registry), AgentThread(registry)):
        assert bridge.submit_task("知乎任务") is True
        assert bridge.submit_task("知乎任务") is False
        assert bridge.busy is True


def test_cross_loop_lock_rejects_second_active_task() -> None:
    name = f"Local\\DesktopAgent.Test.{uuid.uuid4().hex}"
    first = TaskRunLock(name)
    second = TaskRunLock(name)
    third = TaskRunLock(name)

    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
    assert third.acquire() is True
    third.release()


def test_planner_forbids_separate_browser_launch_for_web_tasks() -> None:
    assert "网页任务直接从 navigate 开始" in Planner.SYSTEM_PROMPT
    assert "不要在 navigate 前规划 launch_app" in Planner.SYSTEM_PROMPT
