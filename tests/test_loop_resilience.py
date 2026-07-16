from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.executor import Executor
from src.agent.loop import AgentLoop
from src.agent.state import AgentState
from src.agent.step_guard import GuardResult
from src.schemas.task import RetryPolicy, Step, Task
from src.tools.base import BaseTool, ToolRegistry, ToolSchema
from src.ui.events import EventBus, EventType


class CountingTool(BaseTool):
    def __init__(self, name: str, outcomes: list[bool], error: str = "failed") -> None:
        self.schema = ToolSchema(name=name, description=name, parameters={})
        self.outcomes = outcomes
        self.error = error
        self.calls = 0

    async def execute(self) -> dict:
        index = min(self.calls, len(self.outcomes) - 1)
        success = self.outcomes[index]
        self.calls += 1
        if success:
            return {"success": True, "summary": "ok"}
        return {"success": False, "error": self.error}


class ContinueGuard:
    async def before_step(self, _task, _step):
        return GuardResult()


class ResultVerifier:
    async def check(self, _step, result):
        return result.success, result.error or "ok"

    async def check_task_completion(self, _task, completed):
        return any(result.success for _step, result in completed), "done"


class TestMemory:
    __test__ = False

    def __init__(self) -> None:
        self.working = SimpleNamespace(completed_steps=[], last_result=None)

    def context_for_planner(self) -> str:
        return ""

    def commit(self, step, result) -> None:
        self.working.completed_steps.append((step, result))
        self.working.last_result = result

    def remember_error(self, _reason) -> None:
        pass


def make_loop(registry: ToolRegistry, planner, *, max_steps=20, retry_max=3) -> AgentLoop:
    loop = object.__new__(AgentLoop)
    loop.config = SimpleNamespace(agent=SimpleNamespace(
        max_steps=max_steps, replan_max=3, retry_max=retry_max
    ))
    loop.state = AgentState()
    loop.memory = TestMemory()
    loop.executor = Executor(registry)
    loop.guard = ContinueGuard()
    loop.verifier = ResultVerifier()
    loop.planner = planner
    loop.events = EventBus()
    return loop


@pytest.mark.asyncio
async def test_once_policy_does_not_retry_and_replan_does_not_consume_step_budget() -> None:
    failing = CountingTool("failing", [False])
    recovery = CountingTool("recovery", [True])
    registry = ToolRegistry()
    registry.register(failing)
    registry.register(recovery)

    class Planner:
        async def plan(self, _task, _context):
            return [Step(tool_name="failing", retry_policy=RetryPolicy.ONCE)], ""

        async def replan(self, _task, _step, _reason, _context, _remaining):
            return [Step(tool_name="recovery")], "", False

    loop = make_loop(registry, Planner(), max_steps=1)

    result = await loop.run(Task(goal="recover"))

    assert result.success is True
    assert failing.calls == 1
    assert recovery.calls == 1
    assert loop.state.step_count == 1
    assert not any(e.type == EventType.STEP_RETRY for e in loop.events.replay())


@pytest.mark.asyncio
async def test_retrying_policy_uses_configured_retry_limit() -> None:
    tool = CountingTool("network", [False, False, True])
    registry = ToolRegistry()
    registry.register(tool)

    class Planner:
        async def plan(self, _task, _context):
            return [Step(tool_name="network", retry_policy=RetryPolicy.LINEAR)], ""

    loop = make_loop(registry, Planner(), retry_max=3)
    result = await loop.run(Task(goal="network task"))

    retries = [e for e in loop.events.replay() if e.type == EventType.STEP_RETRY]
    assert result.success is True
    assert tool.calls == 3
    assert len(retries) == 2
    assert all(e.data["max_retries"] == 3 for e in retries)


@pytest.mark.asyncio
async def test_repeated_vision_timeouts_open_circuit_and_stop_retrying() -> None:
    vision = CountingTool(
        "analyze_screen", [False], error="[VISION_TIMEOUT] Request timed out."
    )
    registry = ToolRegistry()
    registry.register(vision)

    class Planner:
        async def plan(self, _task, _context):
            return [Step(
                tool_name="analyze_screen",
                retry_policy=RetryPolicy.LINEAR,
            )], ""

        async def replan(self, _task, _step, _reason, _context, _remaining):
            return [], "vision unavailable", False

    loop = make_loop(registry, Planner(), retry_max=3)
    result = await loop.run(Task(goal="inspect screen"))

    assert result.success is False
    assert vision.calls == 2
    assert loop.state.vision_circuit_open is True


def test_duplicate_wechat_login_confirmations_are_removed() -> None:
    steps = [
        Step(
            tool_name="request_user_input",
            params={"prompt": "请在微信扫码登录后回复已登录"},
            description="确认微信登录",
        ),
        Step(
            tool_name="request_user_input",
            params={"prompt": "微信是否登录？完成手机确认后回复已登录"},
            description="再次确认微信登录状态",
        ),
        Step(
            tool_name="request_user_input",
            params={"prompt": "请确认是否发送文件"},
            description="确认文件发送",
        ),
    ]

    deduped = AgentLoop._dedupe_interactive_steps(steps)

    assert len(deduped) == 2
    assert deduped[0] is steps[0]
    assert deduped[1] is steps[2]


def test_adjacent_duplicate_wechat_shortcuts_are_removed() -> None:
    steps = [
        Step(tool_name="desktop_keypress", params={"keys": "ctrl+f", "app_name": "wechat"}),
        Step(tool_name="desktop_keypress", params={"keys": "ctrl+f", "app_name": "wechat"}),
        Step(tool_name="desktop_keypress", params={"keys": "ctrl+a", "app_name": "wechat"}),
    ]

    deduped = AgentLoop._dedupe_interactive_steps(steps)

    assert [step.params["keys"] for step in deduped] == ["ctrl+f", "ctrl+a"]
