from __future__ import annotations

import pytest

from src.agent.verifier import Verifier
from src.schemas.task import ActionResult, Step


class FailingObserver:
    async def gather(self):
        raise AssertionError("verified native input must not invoke vision")


@pytest.mark.asyncio
async def test_verified_native_keypress_does_not_depend_on_vision() -> None:
    step = Step(
        tool_name="desktop_keypress",
        params={"keys": "ctrl+f", "app_name": "wechat"},
    )
    result = ActionResult(
        step_id=step.id,
        success=True,
        data={
            "foreground_verified": True,
            "target_app": "wechat",
            "window_handle": 123,
        },
    )
    verifier = Verifier(observer=FailingObserver(), llm_client=object())

    success, reason = await verifier.check(step, result)

    assert success is True
    assert "verified foreground" in reason


@pytest.mark.asyncio
async def test_verified_focus_does_not_depend_on_vision() -> None:
    step = Step(
        tool_name="focus_window",
        params={"app_name": "wechat"},
    )
    result = ActionResult(
        step_id=step.id,
        success=True,
        data={"window_handle": 123, "window_title": "微信"},
    )
    verifier = Verifier(observer=FailingObserver(), llm_client=object())

    success, reason = await verifier.check(step, result)

    assert success is True
    assert "focused successfully" in reason
