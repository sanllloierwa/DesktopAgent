"""AgentLoop — 主循环：Plan → Execute → Observe → Verify → (replan|next)"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from loguru import logger

from src.schemas.task import Task, TaskStatus
from src.agent.state import AgentState
from src.agent.memory import MemoryHub
from src.agent.observer import Observer
from src.agent.verifier import Verifier
from src.agent.executor import Executor
from src.agent.planner import Planner
from src.tools.base import ToolRegistry
from src.ui.events import AgentEvent, EventBus, EventType
from src.utils.config import load_config


@dataclass
class LoopResult:
    success: bool
    task: Task
    summary: str = ""
    total_steps: int = 0
    total_duration_ms: float = 0.0


class AgentLoop:
    """桌面 Agent 的主循环引擎。

    使用方式:
        loop = AgentLoop(tool_registry, llm_client)
        result = await loop.run(Task(goal="在知乎发布一篇文章"))

    可选传入 event_bus 以支持 UI 实时展示。
    """

    def __init__(
        self,
        registry: ToolRegistry,
        llm_client: Any,
        event_bus: EventBus | None = None,
    ) -> None:
        self.config = load_config()
        self.state = AgentState()
        self.memory = MemoryHub()
        self.observer = Observer(self.state)
        self.executor = Executor(registry)
        self.verifier = Verifier(self.observer, llm_client)
        self.planner = Planner(llm_client, registry)
        self.events = event_bus or EventBus()

    async def run(self, task: Task) -> LoopResult:
        logger.info(f"AgentLoop starting task: {task.goal}")
        task.mark_running()
        self.events.clear_history()

        # 1. 规划
        await self.events.emit(AgentEvent.plan_start(task.goal))
        context = self.memory.context_for_planner()
        steps = await self.planner.plan(task, context)
        if not steps:
            task.mark_failed()
            await self.events.emit(AgentEvent.error("Planner returned no steps"))
            return LoopResult(success=False, task=task, summary="Planner returned no steps")

        task.steps = steps
        await self.events.emit(AgentEvent.plan_done(steps))
        total_start = time.time()

        # 2. 执行循环
        step_index = 0
        while step_index < len(task.steps):
            step = task.steps[step_index]

            if self.state.step_count >= self.config.agent.max_steps:
                msg = f"Exceeded max steps ({self.config.agent.max_steps})"
                await self.events.emit(AgentEvent.error(msg))
                task.mark_failed()
                await self.events.emit(AgentEvent.task_done(False, msg, self.state.step_count, 0))
                return LoopResult(success=False, task=task, summary=msg)

            if self.state.is_stuck():
                msg = "Agent stuck with consecutive failures"
                await self.events.emit(AgentEvent.error(msg))
                task.mark_failed()
                await self.events.emit(AgentEvent.task_done(False, msg, self.state.step_count, 0))
                return LoopResult(success=False, task=task, summary=msg)

            # 发射步骤开始事件
            await self.events.emit(AgentEvent.step_start(step, step_index + 1, len(task.steps)))

            result = await self.executor.run(step)
            self.memory.commit(step, result)

            # 发射步骤完成事件
            await self.events.emit(AgentEvent.step_done(step, result))

            ok, reason = await self.verifier.check(step, result)
            # 若工具自身成功但 Verifier 判失败，记录这种分歧
            verifier_mismatch_count = 0 if ok else (1 if result.success else 0)

            if not ok:
                logger.warning(f"Step [{step.id}] failed: {reason}")
                self.state.record_failure()
                self.memory.remember_error(reason)

                retries = 0
                while retries < self.config.agent.retry_max and not ok:
                    retries += 1
                    await self.events.emit(
                        AgentEvent.step_retry(step, retries, self.config.agent.retry_max, reason)
                    )
                    result = await self.executor.run(step)
                    self.memory.commit(step, result)
                    await self.events.emit(AgentEvent.step_done(step, result))
                    ok, reason = await self.verifier.check(step, result)
                    # 工具反复成功但 Verifier 反复拒绝 → 信任工具
                    if not ok and result.success:
                        verifier_mismatch_count += 1
                        if verifier_mismatch_count >= 2:
                            logger.warning(
                                f"Step [{step.id}] tool succeeded {verifier_mismatch_count} times "
                                f"but verifier keeps rejecting. Trusting tool result and continuing."
                            )
                            ok = True
                            reason = "Tool succeeded, overriding verifier mismatch"

                if not ok:
                    context = self.memory.context_for_planner()
                    new_steps = await self.planner.replan(task, step, reason, context)
                    if new_steps:
                        task.steps = task.steps[:step_index] + new_steps
                        self.state.record_success()
                        continue
                    else:
                        task.mark_failed()
                        await self.events.emit(AgentEvent.task_done(
                            False, f"Step failed: {reason}", self.state.step_count, 0))
                        return LoopResult(success=False, task=task, summary=f"Step failed: {reason}")

            self.state.record_success()
            step_index += 1

        total_duration = (time.time() - total_start) * 1000
        task.mark_success()
        await self.events.emit(AgentEvent.task_done(
            True, "All steps completed", self.state.step_count, total_duration))
        logger.info(f"Task completed in {self.state.step_count} steps, {total_duration:.0f}ms")
        return LoopResult(
            success=True, task=task,
            summary="All steps completed successfully",
            total_steps=self.state.step_count,
            total_duration_ms=round(total_duration, 1),
        )

    def reset(self) -> None:
        self.state = AgentState()
        self.memory.clear()
        self.observer = Observer(self.state)
