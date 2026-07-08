"""AgentLoop — 主循环：Plan → Execute → Observe → Verify → (replan|next)"""

from __future__ import annotations

import time
import re
from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from src.schemas.task import Task, TaskStatus
from src.agent.state import AgentState
from src.agent.memory import MemoryHub
from src.agent.observer import Observer
from src.agent.verifier import Verifier
from src.agent.executor import Executor
from src.agent.planner import Planner
from src.agent.step_guard import GuardDecision, StepGuard
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
        self.guard = StepGuard(self.executor, self.memory)
        self.verifier = Verifier(self.observer, llm_client)
        self.planner = Planner(llm_client, registry)
        self.events = event_bus or EventBus()

    def _lookup_previous_value(self, key: str) -> Any:
        key = key.strip()
        for prefix in ("last_result.", "last.", "data."):
            if key.startswith(prefix):
                key = key[len(prefix):]

        for _step, result in reversed(self.memory.working.completed_steps):
            if isinstance(result.data, dict) and key in result.data:
                return result.data[key]
            if key == "summary":
                return result.summary
        return None

    def _resolve_param_refs(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._resolve_param_refs(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_param_refs(v) for v in value]
        if not isinstance(value, str):
            return value

        pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")
        exact_match = pattern.fullmatch(value)
        if exact_match:
            resolved = self._lookup_previous_value(exact_match.group(1))
            return value if resolved is None else resolved

        def replace_match(match: re.Match[str]) -> str:
            resolved = self._lookup_previous_value(match.group(1))
            return match.group(0) if resolved is None else str(resolved)

        return pattern.sub(replace_match, value)

    def _runtime_step(self, step: Any) -> Any:
        resolved_params = self._resolve_param_refs(step.params)
        if resolved_params == step.params:
            return step
        return replace(step, params=resolved_params)

    async def run(self, task: Task) -> LoopResult:
        logger.info(f"AgentLoop starting task: {task.goal}")
        task.mark_running()
        self.events.clear_history()

        # 1. 规划
        await self.events.emit(AgentEvent.plan_start(task.goal))
        context = self.memory.context_for_planner()
        steps, fallback = await self.planner.plan(task, context)
        if not steps:
            task.mark_failed()
            reason = fallback or "Planner returned no steps"
            await self.events.emit(AgentEvent.error(reason))
            return LoopResult(success=False, task=task, summary=reason)

        task.steps = steps
        await self.events.emit(AgentEvent.plan_done(steps))
        total_start = time.time()

        # 2. 执行循环
        step_index = 0
        replan_count = 0  # 跟踪连续 replan 次数，防止无限循环
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

            guard_result = await self.guard.before_step(task, step)
            if guard_result.action_result:
                await self.events.emit(AgentEvent.log(f"Guard: {guard_result.action_result.summary}"))

            if guard_result.decision == GuardDecision.COMPLETE:
                total_duration = (time.time() - total_start) * 1000
                summary = guard_result.reason or "Task completed by guard"
                task.mark_success()
                await self.events.emit(AgentEvent.task_done(
                    True, summary, self.state.step_count, total_duration))
                logger.info(f"Task completed by guard: {summary}")
                return LoopResult(
                    success=True,
                    task=task,
                    summary=summary,
                    total_steps=self.state.step_count,
                    total_duration_ms=round(total_duration, 1),
                )

            if guard_result.decision == GuardDecision.SKIP:
                logger.info(f"Guard skipped step [{step.id}]: {guard_result.reason}")
                await self.events.emit(AgentEvent.log(f"Skip: {guard_result.reason}"))
                step_index += 1
                continue

            if guard_result.decision == GuardDecision.REPLAN:
                replan_count += 1
                if replan_count > self.config.agent.replan_max:
                    msg = f"Replan limit exceeded ({self.config.agent.replan_max}), task cannot be completed"
                    await self.events.emit(AgentEvent.error(msg))
                    task.mark_failed()
                    await self.events.emit(AgentEvent.task_done(False, msg, self.state.step_count, 0))
                    return LoopResult(success=False, task=task, summary=msg)

                logger.info(f"Guard requested replan before step [{step.id}]: {guard_result.reason}")
                await self.events.emit(AgentEvent.log(f"Replan before step: {guard_result.reason}"))
                context = self.memory.context_for_planner()
                remaining = task.steps[step_index + 1:]
                new_steps, fallback = await self.planner.replan(
                    task, step, guard_result.reason, context, remaining
                )
                if new_steps:
                    task.steps = task.steps[:step_index] + new_steps + remaining
                    await self.events.emit(AgentEvent.plan_done(task.steps))
                    continue
                summary = fallback or guard_result.reason
                task.mark_failed()
                await self.events.emit(AgentEvent.task_done(False, summary, self.state.step_count, 0))
                return LoopResult(success=False, task=task, summary=summary)

            # 发射步骤开始事件
            await self.events.emit(AgentEvent.step_start(step, step_index + 1, len(task.steps)))

            runtime_step = self._runtime_step(step)
            result = await self.executor.run(runtime_step)
            self.memory.commit(step, result)

            # 发射步骤完成事件（包含错误信息）
            await self.events.emit(AgentEvent.step_done(step, result))

            ok, reason = await self.verifier.check(step, result)
            # 若工具自身成功但 Verifier 判失败，记录这种分歧
            verifier_mismatch_count = 0 if ok else (1 if result.success else 0)

            if ok and isinstance(result.data, dict) and result.data.get("task_complete"):
                total_duration = (time.time() - total_start) * 1000
                summary = result.summary or "Task completed by tool signal"
                self.state.record_success()
                task.mark_success()
                await self.events.emit(AgentEvent.task_done(
                    True, summary, self.state.step_count, total_duration))
                logger.info(f"Task completed early: {summary}")
                return LoopResult(
                    success=True,
                    task=task,
                    summary=summary,
                    total_steps=self.state.step_count,
                    total_duration_ms=round(total_duration, 1),
                )

            if not ok:
                logger.warning(f"Step [{step.id}] failed: {reason}")
                self.state.record_failure()
                self.memory.remember_error(reason)

                retries = 0
                user_cancelled = False
                user_timeout = False
                # 认证/授权错误重试无意义，直接跳过重试阶段
                if reason.startswith("[AUTH_ERR]"):
                    reason = reason.replace("[AUTH_ERR] ", "")
                    retries = self.config.agent.retry_max
                # 用户取消 — 跳过重试且不进入 replan
                if reason.startswith("[USER_CANCELLED]"):
                    reason = reason.replace("[USER_CANCELLED] ", "")
                    retries = self.config.agent.retry_max
                    user_cancelled = True
                # 用户超时 — 跳过重试但可以 replan
                if reason.startswith("[USER_TIMEOUT]"):
                    reason = reason.replace("[USER_TIMEOUT] ", "")
                    retries = self.config.agent.retry_max
                    user_timeout = True
                while retries < self.config.agent.retry_max and not ok:
                    retries += 1
                    await self.events.emit(
                        AgentEvent.step_retry(step, retries, self.config.agent.retry_max, reason)
                    )
                    result = await self.executor.run(runtime_step)
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
                    # 用户取消 — 立即失败，不进入 replan
                    if user_cancelled:
                        msg = f"用户取消: {reason}"
                        logger.warning(msg)
                        await self.events.emit(AgentEvent.error(msg))
                        task.mark_failed()
                        await self.events.emit(AgentEvent.task_done(
                            False, msg, self.state.step_count, 0))
                        return LoopResult(success=False, task=task, summary=msg)

                    # 用户超时 / 其他失败 — 进入 replan
                    replan_count += 1
                    if replan_count > self.config.agent.replan_max:
                        msg = f"Replan limit exceeded ({self.config.agent.replan_max}), task cannot be completed"
                        logger.error(msg)
                        await self.events.emit(AgentEvent.error(msg))
                        task.mark_failed()
                        await self.events.emit(AgentEvent.task_done(False, msg, self.state.step_count, 0))
                        return LoopResult(success=False, task=task, summary=msg)

                    context = self.memory.context_for_planner()
                    remaining = task.steps[step_index + 1:]  # 失败步骤之后的原始步骤
                    new_steps, fallback = await self.planner.replan(task, step, reason, context, remaining)
                    if new_steps:
                        # 保留原始剩余步骤，避免被 replan 截断整个计划
                        task.steps = task.steps[:step_index] + new_steps + remaining
                        logger.info(
                            f"Replan merged: {len(new_steps)} new + {len(remaining)} remaining = "
                            f"{len(new_steps) + len(remaining)} total steps ahead"
                        )
                        self.state.record_success()
                        continue
                    else:
                        task.mark_failed()
                        summary = fallback or f"Step failed: {reason}"
                        await self.events.emit(AgentEvent.task_done(
                            False, summary, self.state.step_count, 0))
                        return LoopResult(success=False, task=task, summary=summary)

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
        self.guard = StepGuard(self.executor, self.memory)
