from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agent.executor import Executor
from src.agent.loop import AgentLoop
from src.agent.state import AgentState
from src.agent.step_guard import GuardResult
from src.agent.verifier import Verifier
from src.schemas.task import ActionResult, Step, Task
from src.tools.base import BaseTool, ToolRegistry, ToolSchema
from src.ui.events import EventBus


class ObserveTool(BaseTool):
    schema = ToolSchema(name="analyze_screen", description="observe", parameters={})

    async def execute(self) -> dict:
        return {"success": True, "summary": "微信窗口已打开", "answer": "已登录"}


class FakePlanner:
    async def plan(self, _task, _context):
        return [Step(tool_name="analyze_screen", description="分析微信状态")], ""

    async def replan(self, _task, _step, _reason, _context, _remaining):
        return [], "缺少发送消息所需的桌面操作步骤", False


class FakeGuard:
    async def before_step(self, _task, _step):
        return GuardResult()


class FakeVerifier:
    async def check(self, _step, _result):
        return True, "step ok"

    async def check_task_completion(self, _task, _completed):
        return False, "仅确认登录，尚未进入群聊并发送 demo"


class FakeMemory:
    def __init__(self) -> None:
        self.working = SimpleNamespace(completed_steps=[], last_result=None)

    def context_for_planner(self) -> str:
        return ""

    def commit(self, step, result) -> None:
        self.working.completed_steps.append((step, result))
        self.working.last_result = result

    def remember_error(self, _reason) -> None:
        pass


@pytest.mark.asyncio
async def test_loop_does_not_report_done_for_observation_only_plan() -> None:
    registry = ToolRegistry()
    registry.register(ObserveTool())
    loop = object.__new__(AgentLoop)
    loop.config = SimpleNamespace(agent=SimpleNamespace(
        max_steps=20, replan_max=1, retry_max=0
    ))
    loop.state = AgentState()
    loop.memory = FakeMemory()
    loop.executor = Executor(registry)
    loop.guard = FakeGuard()
    loop.verifier = FakeVerifier()
    loop.planner = FakePlanner()
    loop.events = EventBus()

    result = await loop.run(Task(goal="进入微信群‘实习’并发送 demo"))

    assert result.success is False
    assert "缺少发送消息" in result.summary


class CompletionLLM:
    model = "test-model"

    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.messages = self
        self.last_prompt = ""

    async def create(self, **kwargs):
        self.last_prompt = kwargs["messages"][0]["content"]
        return SimpleNamespace(content=[SimpleNamespace(text=self.answer)])


@pytest.mark.asyncio
async def test_final_verifier_requires_submitted_message_evidence() -> None:
    llm = CompletionLLM(
        '{"success": false, "reason": "只粘贴了 demo，缺少 Enter 提交证据"}'
    )
    verifier = Verifier(observer=None, llm_client=llm)
    step = Step(
        tool_name="desktop_type_text",
        description="在实习群输入 demo",
    )
    result = ActionResult(
        step_id=step.id,
        success=True,
        summary="Typed 4 chars via clipboard paste",
    )

    ok, reason = await verifier.check_task_completion(
        Task(goal="进入群聊‘实习’发送 demo"), [(step, result)]
    )

    assert ok is False
    assert "Enter" in reason
    assert "仅粘贴文字不等于已发送" in llm.last_prompt
