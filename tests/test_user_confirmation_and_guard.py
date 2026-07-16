from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.step_guard import GuardDecision, StepGuard
from src.schemas.task import Step, Task
from src.tools.interactive.user_input import normalize_confirmation


@pytest.mark.parametrize("value", ["是", "已登录", "已发送", "yes", "OK"])
def test_positive_confirmation_is_normalized(value: str) -> None:
    assert normalize_confirmation(value) == "yes"


@pytest.mark.parametrize("value", ["否", "未发送", "no"])
def test_negative_confirmation_is_normalized(value: str) -> None:
    assert normalize_confirmation(value) == "no"


def test_arbitrary_input_is_not_treated_as_confirmation() -> None:
    assert normalize_confirmation("当前窗口标题不是实习") == "unknown"


@pytest.mark.asyncio
async def test_wechat_input_without_target_app_requests_replan() -> None:
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[]))
    guard = StepGuard(executor=None, memory=memory)
    step = Step(tool_name="desktop_type_text", params={"text": "demo"})

    result = await guard.before_step(Task(goal="在微信群发送 demo"), step)

    assert result.decision == GuardDecision.REPLAN
    assert "[TARGET_REQUIRED]" in result.reason


@pytest.mark.asyncio
async def test_wechat_input_with_verified_target_can_continue() -> None:
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[]))
    guard = StepGuard(executor=None, memory=memory)
    step = Step(
        tool_name="desktop_type_text",
        params={"text": "demo", "app_name": "wechat"},
    )

    result = await guard.before_step(Task(goal="在微信群发送 demo"), step)

    assert result.decision == GuardDecision.CONTINUE
