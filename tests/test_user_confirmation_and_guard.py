from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.step_guard import GuardDecision, StepGuard
from src.schemas.task import ActionResult, Step, Task
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


@pytest.mark.asyncio
async def test_wechat_click_without_target_app_requests_replan() -> None:
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[]))
    guard = StepGuard(executor=None, memory=memory)
    step = Step(tool_name="desktop_click", params={"x": 100, "y": 200})

    result = await guard.before_step(Task(goal="在微信中关注服务号"), step)

    assert result.decision == GuardDecision.REPLAN
    assert "[TARGET_REQUIRED]" in result.reason


@pytest.mark.asyncio
async def test_wechat_screenshot_without_target_app_requests_replan() -> None:
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[]))
    guard = StepGuard(executor=None, memory=memory)

    result = await guard.before_step(
        Task(goal="在微信中关注服务号"),
        Step(tool_name="desktop_screenshot"),
    )

    assert result.decision == GuardDecision.REPLAN
    assert "[TARGET_REQUIRED]" in result.reason


@pytest.mark.asyncio
async def test_wechat_relaunch_is_skipped_after_session_started() -> None:
    focused = Step(
        tool_name="focus_window",
        params={"app_name": "wechat"},
    )
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[(
        focused,
        ActionResult(
            step_id=focused.id,
            success=True,
            data={"window_handle": 123, "target_app": "wechat"},
        ),
    )]))
    guard = StepGuard(executor=None, memory=memory)

    result = await guard.before_step(
        Task(goal="在微信中关注服务号"),
        Step(tool_name="launch_app", params={"app_name": "wechat"}),
    )

    assert result.decision == GuardDecision.SKIP
    assert "跳过重复启动" in result.reason


@pytest.mark.asyncio
async def test_wechat_close_recovery_is_skipped() -> None:
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[]))
    guard = StepGuard(executor=None, memory=memory)

    result = await guard.before_step(
        Task(goal="在微信中关注服务号"),
        Step(tool_name="close_app", params={"app_name": "wechat"}),
    )

    assert result.decision == GuardDecision.SKIP
    assert "保留当前登录会话" in result.reason


@pytest.mark.asyncio
async def test_confirmed_wechat_login_screen_requests_login_recovery() -> None:
    analyzed = Step(tool_name="analyze_screen", description="分析微信界面")
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[(
        analyzed,
        ActionResult(
            step_id=analyzed.id,
            success=True,
            summary="Analysis: 微信客户端尚未登录",
            data={"answer": "当前始终停留在扫码登录界面，并未进入主界面"},
        ),
    )]))
    guard = StepGuard(executor=None, memory=memory)

    result = await guard.before_step(
        Task(goal="微信搜索火眼审阅服务号"),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "ctrl+f", "app_name": "wechat"},
        ),
    )

    assert result.decision == GuardDecision.REPLAN
    assert "[WECHAT_LOGIN_REQUIRED]" in result.reason


@pytest.mark.asyncio
async def test_wechat_main_screen_does_not_repeat_login_recovery() -> None:
    analyzed = Step(
        tool_name="check_wechat_login_status",
        description="结构化复验微信登录",
    )
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[(
        analyzed,
        ActionResult(
            step_id=analyzed.id,
            success=True,
            data={"logged_in": True, "state": "main_ui"},
        ),
    )]))
    guard = StepGuard(executor=None, memory=memory)

    result = await guard.before_step(
        Task(goal="微信搜索火眼审阅服务号"),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "ctrl+f", "app_name": "wechat"},
        ),
    )

    assert result.decision == GuardDecision.CONTINUE


@pytest.mark.asyncio
async def test_structured_logged_out_state_blocks_wechat_search() -> None:
    checked = Step(
        tool_name="check_wechat_login_status",
        description="结构化检查微信登录",
    )
    memory = SimpleNamespace(working=SimpleNamespace(completed_steps=[(
        checked,
        ActionResult(
            step_id=checked.id,
            success=True,
            data={
                "logged_in": False,
                "state": "login_required",
                "reason": "当前是登录/进入界面",
            },
        ),
    )]))
    guard = StepGuard(executor=None, memory=memory)

    result = await guard.before_step(
        Task(goal="微信搜索火眼审阅服务号"),
        Step(
            tool_name="desktop_keypress",
            params={"keys": "ctrl+f", "app_name": "wechat"},
        ),
    )

    assert result.decision == GuardDecision.REPLAN
    assert "[WECHAT_LOGIN_REQUIRED]" in result.reason


class LoginStatusExecutor:
    async def run(self, step: Step) -> ActionResult:
        return ActionResult(
            step_id=step.id,
            success=True,
            summary="Already logged in",
            data={"logged_in": True, "login_satisfied": True},
        )


class GuardMemory:
    def __init__(self) -> None:
        navigate = Step(tool_name="navigate")
        self.working = SimpleNamespace(completed_steps=[(
            navigate,
            ActionResult(step_id=navigate.id, success=True, summary="opened"),
        )])

    def commit(self, step: Step, result: ActionResult) -> None:
        self.working.completed_steps.append((step, result))


@pytest.mark.asyncio
async def test_logged_in_skips_only_login_step_in_compound_task() -> None:
    memory = GuardMemory()
    guard = StepGuard(executor=LoginStatusExecutor(), memory=memory)
    login_step = Step(
        tool_name="request_user_input",
        params={"prompt": "请输入知乎登录手机号"},
        description="请求登录手机号",
    )

    result = await guard.before_step(
        Task(goal="登录知乎后写文章并发布"), login_step
    )

    assert result.decision == GuardDecision.SKIP
    assert result.decision != GuardDecision.COMPLETE


@pytest.mark.asyncio
async def test_logged_in_continues_non_login_step_in_compound_task() -> None:
    memory = GuardMemory()
    guard = StepGuard(executor=LoginStatusExecutor(), memory=memory)
    writing_step = Step(
        tool_name="generate_article",
        params={"topic": "视觉 Agent"},
        description="生成文章",
    )

    result = await guard.before_step(
        Task(goal="登录知乎后写文章并发布"), writing_step
    )

    assert result.decision == GuardDecision.CONTINUE
    assert result.decision != GuardDecision.COMPLETE


@pytest.mark.asyncio
async def test_logged_in_state_skips_stale_login_dom_checks() -> None:
    check = Step(tool_name="check_login_status")
    memory = SimpleNamespace(
        working=SimpleNamespace(completed_steps=[(
            check,
            ActionResult(
                step_id=check.id,
                success=True,
                data={"logged_in": True, "login_satisfied": True},
            ),
        )]),
    )
    guard = StepGuard(executor=None, memory=memory)

    decision = await guard.before_step(
        Task(goal="登录知乎后写文章并发布"),
        Step(tool_name="get_dom", description="获取登录页面DOM并确认登录方式"),
    )

    assert decision.decision == GuardDecision.SKIP
