"""AgentLoop — 主循环：Plan → Execute → Observe → Verify → (replan|next)"""

from __future__ import annotations

import asyncio
import time
import re
from dataclasses import dataclass, replace
from typing import Any

from loguru import logger

from src.schemas.task import ActionResult, RetryPolicy, Step, Task
from src.agent.state import AgentState
from src.agent.memory import MemoryHub
from src.agent.observer import Observer
from src.agent.verifier import Verifier
from src.agent.executor import Executor
from src.agent.planner import Planner
from src.agent.step_guard import GuardDecision, StepGuard
from src.agent.run_lock import TaskRunLock
from src.tools.base import ToolRegistry
from src.ui.events import AgentEvent, EventBus
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
        for prefix in ("last_result.", "last."):
            if key.startswith(prefix):
                return self._lookup_in_result(self.memory.working.last_result, key[len(prefix):])

        if key.startswith("data."):
            key = key[len("data."):]

        if "." in key:
            tool_name, field = key.split(".", 1)
            for step, result in reversed(self.memory.working.completed_steps):
                if step.tool_name == tool_name:
                    value = self._lookup_in_result(result, field)
                    if value is not None:
                        return value

        if key.endswith("_result"):
            tool_name = key[:-len("_result")]
            for step, result in reversed(self.memory.working.completed_steps):
                if step.tool_name == tool_name:
                    return self._primary_result_value(result)

        for step, result in reversed(self.memory.working.completed_steps):
            value = self._lookup_in_result(result, key)
            if value is not None:
                return value
            if key == step.tool_name:
                return self._primary_result_value(result)

        if key == "user_input":
            for step, result in reversed(self.memory.working.completed_steps):
                if step.tool_name in {"generate_article", "summarize", "extract_text", "analyze_screen"}:
                    value = self._primary_result_value(result)
                    if value is not None:
                        logger.warning(
                            "Resolved {{user_input}} from previous content result because no "
                            "request_user_input result exists."
                        )
                        return value
        return None

    def _lookup_in_result(self, result: ActionResult | None, key: str) -> Any:
        if result is None:
            return None
        if key in {"screenshot", "screenshot_base64", "image_base64"}:
            return result.screenshot_base64
        if isinstance(result.data, dict) and key in result.data:
            return result.data[key]
        if isinstance(result.data, dict):
            tokens = re.findall(r"([a-zA-Z0-9_-]+)|\[(\d+)\]", key)
            current: Any = result.data
            for field, index in tokens:
                if field:
                    if not isinstance(current, dict) or field not in current:
                        current = None
                        break
                    current = current[field]
                else:
                    position = int(index)
                    if (
                        not isinstance(current, (list, tuple))
                        or position >= len(current)
                    ):
                        current = None
                        break
                    current = current[position]
            if current is not None and tokens:
                return current
        if key == "summary":
            return result.summary
        if key in {"result", "output", "value", "text", "content"}:
            return self._primary_result_value(result)
        return None

    def _primary_result_value(self, result: ActionResult) -> Any:
        if isinstance(result.data, dict):
            for key in (
                "article",
                "result",
                "user_input",
                "text",
                "content",
                "answer",
                "screenshot_base64",
                "summary",
                "filepath",
                "url",
            ):
                value = result.data.get(key)
                if value not in (None, ""):
                    return value
        if result.screenshot_base64:
            return result.screenshot_base64
        return result.summary or None

    def _resolve_param_refs(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._resolve_param_refs(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve_param_refs(v) for v in value]
        if not isinstance(value, str):
            return value

        pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-\[\]]+)\s*\}\}")
        exact_match = pattern.fullmatch(value)
        if exact_match:
            resolved = self._lookup_previous_value(exact_match.group(1))
            return value if resolved is None else resolved

        def replace_match(match: re.Match[str]) -> str:
            resolved = self._lookup_previous_value(match.group(1))
            return match.group(0) if resolved is None else str(resolved)

        return pattern.sub(replace_match, value)

    def _unresolved_param_refs(self, value: Any) -> list[str]:
        refs: list[str] = []
        if isinstance(value, dict):
            for item in value.values():
                refs.extend(self._unresolved_param_refs(item))
            return refs
        if isinstance(value, list):
            for item in value:
                refs.extend(self._unresolved_param_refs(item))
            return refs
        if not isinstance(value, str):
            return refs

        pattern = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-\[\]]+)\s*\}\}")
        return [match.group(1) for match in pattern.finditer(value)]

    def _runtime_step(self, step: Any) -> Any:
        resolved_params = self._resolve_param_refs(step.params)
        if resolved_params == step.params:
            return step
        return replace(step, params=resolved_params)

    def _retry_limit(self, step: Step) -> int:
        """Return retry attempts after the initial call for this step policy."""
        if step.tool_name == "publish_article":
            # Publishing is irreversible. PublishArticleTool already waits
            # through navigation races and must never be clicked repeatedly by
            # the generic retry loop.
            return 0
        if step.retry_policy == RetryPolicy.ONCE:
            return 0
        return max(0, int(self.config.agent.retry_max))

    @staticmethod
    def _is_vision_step(step: Step) -> bool:
        return step.tool_name in {
            "analyze_screen",
            "check_wechat_login_status",
            "locate_screen_element",
            "open_wechat_search_candidate",
        }

    @staticmethod
    def _is_vision_timeout(reason: str) -> bool:
        lowered = reason.lower()
        return reason.startswith("[VISION_TIMEOUT]") or (
            "timed out" in lowered and "vision" in lowered
        ) or "request timed out" in lowered

    def _record_vision_failure(self, step: Step, reason: str) -> None:
        if self._is_vision_step(step) and self._is_vision_timeout(reason):
            self.state.record_vision_timeout()
            if self.state.vision_circuit_open:
                logger.warning("Vision circuit opened after repeated timeouts")

    @staticmethod
    def _interactive_step_key(step: Step) -> str | None:
        if step.tool_name != "request_user_input":
            return None
        text = " ".join((
            step.description,
            step.expected_outcome,
            str(step.params.get("prompt", "")),
        )).strip().lower()
        compact = re.sub(r"[\s，。！？；：、'\"“”‘’()（）]+", "", text)
        if any(word in text for word in ("微信", "wechat", "weixin")) and any(
            word in text for word in ("登录", "扫码", "手机确认")
        ):
            return "request_user_input:wechat_login_confirmation"
        return f"request_user_input:{compact}"

    @classmethod
    def _dedupe_interactive_steps(cls, steps: list[Step]) -> list[Step]:
        """Remove repeated prompts and adjacent duplicate desktop key actions."""
        seen: set[str] = set()
        result: list[Step] = []
        for step in steps:
            key = cls._interactive_step_key(step)
            if key is not None:
                if key in seen:
                    logger.info(f"Dropped duplicate interactive step: {step.description}")
                    continue
                seen.add(key)
            if step.tool_name == "desktop_keypress" and result:
                previous = result[-1]
                if previous.tool_name == "desktop_keypress":
                    current_action = (
                        str(step.params.get("keys", "")).strip().lower(),
                        str(step.params.get("app_name", "")).strip().lower(),
                        int(step.params.get("presses", 1)),
                    )
                    previous_action = (
                        str(previous.params.get("keys", "")).strip().lower(),
                        str(previous.params.get("app_name", "")).strip().lower(),
                        int(previous.params.get("presses", 1)),
                    )
                    if current_action == previous_action:
                        logger.info(f"Dropped duplicate desktop keypress: {current_action}")
                        continue
            result.append(step)
        return result

    @staticmethod
    def _merge_replan(
        current_steps: list[Any],
        step_index: int,
        new_steps: list[Any],
        preserve_remaining: bool,
    ) -> list[Any]:
        """替换失败步骤，并按 Planner 声明决定是否保留旧后续步骤。"""
        remaining = current_steps[step_index + 1:] if preserve_remaining else []
        merged = current_steps[:step_index] + new_steps + remaining
        return AgentLoop._dedupe_interactive_steps(merged)

    async def _execute_runtime_step(self, step: Any) -> ActionResult:
        if self._is_vision_step(step) and self.state.vision_circuit_open:
            msg = (
                "[VISION_UNAVAILABLE] Vision disabled for the current task after "
                "repeated timeouts; use one manual confirmation and continue with "
                "non-visual desktop tools"
            )
            return ActionResult(
                step_id=step.id,
                success=False,
                error=msg,
                summary=msg,
            )
        unresolved_refs = self._unresolved_param_refs(step.params)
        if unresolved_refs:
            refs = ", ".join(sorted(set(unresolved_refs)))
            msg = f"Unresolved parameter reference(s): {refs}"
            logger.warning(f"Step [{step.id}] blocked before execution: {msg}")
            return ActionResult(
                step_id=step.id,
                success=False,
                error=msg,
                summary=msg,
            )
        return await self.executor.run(step)

    async def _refresh_perception(self, step: Step, result: ActionResult) -> None:
        """Feed tool observations back into AgentState before verification.

        Browser actions also receive a fresh DOM snapshot so the verifier does
        not judge a click/type operation from a stale pre-action page.
        """
        if step.tool_name == "get_dom" and result.success:
            self.state.last_dom_summary = result.summary
        if result.screenshot_base64:
            self.state.last_screenshot_base64 = result.screenshot_base64
        if isinstance(result.data, dict):
            url = result.data.get("page_url") or result.data.get("url")
            if isinstance(url, str) and url:
                self.state.last_browser_url = url

        if not result.success or step.tool_name not in {"navigate", "click", "type_text", "upload_image"}:
            return
        dom_tool = self.executor.registry.get("get_dom")
        if dom_tool is None:
            return
        try:
            snapshot = await dom_tool.execute()
            if isinstance(snapshot, dict) and snapshot.get("success"):
                self.state.last_dom_summary = str(snapshot.get("summary", ""))
        except Exception as exc:
            logger.debug(f"Post-action DOM refresh skipped: {exc}")

    async def _verify_manual_login_confirmation(
        self,
        task: Task,
        step: Step,
        result: ActionResult,
    ) -> ActionResult | None:
        """Re-check the webpage after a user says a manual login is complete."""
        if step.tool_name != "request_user_input" or not result.success:
            return None
        if not isinstance(result.data, dict) or result.data.get("confirmation") != "yes":
            return None
        text = " ".join((
            task.goal,
            step.description,
            step.expected_outcome,
            str(step.params.get("prompt", "")),
        )).lower()
        if not any(term in text for term in ("登录", "登陆", "login", "sign in")):
            return None
        if any(term in text for term in ("微信", "wechat", "weixin")):
            # Native WeChat login cannot be checked by the browser-only
            # check_login_status tool. The deterministic recovery plan follows
            # this prompt with a targeted screenshot and screen analysis.
            return None

        platform = "zhihu" if ("知乎" in text or "zhihu" in text) else "generic"
        check = Step(
            tool_name="check_login_status",
            params={"platform": platform},
            description=f"人工确认后重新检查 {platform} 登录状态",
            expected_outcome="网页已显示登录后的用户控件",
        )
        check_result: ActionResult | None = None
        for attempt in range(4):
            check_result = await self.executor.run(check)
            self.memory.commit(check, check_result)
            await self._refresh_perception(check, check_result)
            logged_in = (
                check_result.success
                and isinstance(check_result.data, dict)
                and check_result.data.get("logged_in") is True
            )
            if logged_in:
                break
            if attempt < 3:
                await asyncio.sleep(0.5)
        assert check_result is not None
        await self.events.emit(AgentEvent.log(
            "人工登录复查："
            + (
                "已确认登录成功"
                if (
                    check_result.success
                    and isinstance(check_result.data, dict)
                    and check_result.data.get("logged_in") is True
                )
                else "尚未检测到登录后的页面状态"
            )
        ))
        return check_result

    async def run(self, task: Task) -> LoopResult:
        run_lock = TaskRunLock()
        if not run_lock.acquire():
            reason = "已有一个 Desktop Agent 任务正在运行，已拒绝重复提交"
            logger.warning(reason)
            task.mark_failed()
            await self.events.emit(AgentEvent.error(reason))
            await self.events.emit(AgentEvent.task_done(False, reason, 0, 0))
            return LoopResult(success=False, task=task, summary=reason)
        try:
            return await self._run_task(task)
        finally:
            run_lock.release()

    async def _run_task(self, task: Task) -> LoopResult:
        logger.info(f"AgentLoop starting task: {task.goal}")
        task.mark_running()
        self.state.vision_timeout_failures = 0
        self.state.vision_circuit_open = False
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

        task.steps = self._dedupe_interactive_steps(steps)
        await self.events.emit(AgentEvent.plan_done(task.steps))
        total_start = time.time()

        # 2. 执行循环
        step_index = 0
        replan_count = 0  # 跟踪连续 replan 次数，防止无限循环
        while True:
            if step_index >= len(task.steps):
                goal_ok, goal_reason = await self.verifier.check_task_completion(
                    task, self.memory.working.completed_steps
                )
                if goal_ok:
                    logger.info(f"Final goal verified: {goal_reason}")
                    break

                replan_count += 1
                if replan_count > self.config.agent.replan_max:
                    msg = f"Final goal not achieved: {goal_reason}"
                    logger.error(msg)
                    await self.events.emit(AgentEvent.error(msg))
                    task.mark_failed()
                    await self.events.emit(AgentEvent.task_done(
                        False, msg, self.state.step_count, 0
                    ))
                    return LoopResult(
                        success=False,
                        task=task,
                        summary=msg,
                        total_steps=self.state.step_count,
                    )

                logger.warning(f"Plan exhausted before goal completion: {goal_reason}")
                await self.events.emit(AgentEvent.log(
                    f"Final goal incomplete, replanning: {goal_reason}"
                ))
                completion_gap = Step(
                    tool_name="goal_completion",
                    params={},
                    description="补齐尚未完成的用户最终目标",
                    expected_outcome=task.goal,
                )
                context = self.memory.context_for_planner()
                new_steps, fallback, _ = await self.planner.replan(
                    task, completion_gap, goal_reason, context, []
                )
                if not new_steps:
                    msg = fallback or f"Final goal not achieved: {goal_reason}"
                    task.mark_failed()
                    await self.events.emit(AgentEvent.task_done(
                        False, msg, self.state.step_count, 0
                    ))
                    return LoopResult(
                        success=False,
                        task=task,
                        summary=msg,
                        total_steps=self.state.step_count,
                    )

                task.steps = self._dedupe_interactive_steps(task.steps + new_steps)
                await self.events.emit(AgentEvent.plan_done(task.steps))
                continue

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
                new_steps, fallback, preserve_remaining = await self.planner.replan(
                    task, step, guard_result.reason, context, remaining
                )
                if new_steps:
                    task.steps = self._merge_replan(
                        task.steps, step_index, new_steps, preserve_remaining
                    )
                    await self.events.emit(AgentEvent.plan_done(task.steps))
                    continue
                summary = fallback or guard_result.reason
                task.mark_failed()
                await self.events.emit(AgentEvent.task_done(False, summary, self.state.step_count, 0))
                return LoopResult(success=False, task=task, summary=summary)

            # 发射步骤开始事件
            await self.events.emit(AgentEvent.step_start(step, step_index + 1, len(task.steps)))

            runtime_step = self._runtime_step(step)
            result = await self._execute_runtime_step(runtime_step)
            self.memory.commit(step, result)
            await self._refresh_perception(runtime_step, result)
            manual_login_check = await self._verify_manual_login_confirmation(
                task, runtime_step, result
            )

            # 发射步骤完成事件（包含错误信息）
            await self.events.emit(AgentEvent.step_done(step, result))

            ok, reason = await self.verifier.check(step, result)
            if manual_login_check is not None:
                logged_in = (
                    manual_login_check.success
                    and isinstance(manual_login_check.data, dict)
                    and manual_login_check.data.get("logged_in") is True
                )
                if not logged_in:
                    ok = False
                    reason = (
                        "用户已确认手动登录，但当前网页仍未检测到登录后的用户控件；"
                        "请确认浏览器已进入知乎登录后的页面"
                    )

            if (
                ok
                and isinstance(result.data, dict)
                and result.data.get("task_complete") is True
                and result.data.get("completion_scope") == "task"
            ):
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
                self._record_vision_failure(step, reason)

                retries = 0
                retry_limit = self._retry_limit(step)
                user_cancelled = False
                env_error = False
                # 认证/授权错误重试无意义，直接跳过重试阶段
                if reason.startswith("[AUTH_ERR]"):
                    reason = reason.replace("[AUTH_ERR] ", "")
                    retries = retry_limit
                if reason.startswith("[ENV_ERR]"):
                    reason = reason.replace("[ENV_ERR] ", "")
                    retries = retry_limit
                    env_error = True
                # 用户取消 — 跳过重试且不进入 replan
                if reason.startswith("[USER_CANCELLED]"):
                    reason = reason.replace("[USER_CANCELLED] ", "")
                    retries = retry_limit
                    user_cancelled = True
                # 用户超时 — 跳过重试但可以 replan
                if reason.startswith("[USER_TIMEOUT]"):
                    reason = reason.replace("[USER_TIMEOUT] ", "")
                    retries = retry_limit
                if reason.startswith("[VISION_UNAVAILABLE]"):
                    retry_limit = 0
                if self._is_vision_timeout(reason) and self.state.vision_circuit_open:
                    retry_limit = 0
                while retries < retry_limit and not ok:
                    retries += 1
                    await self.events.emit(
                        AgentEvent.step_retry(step, retries, retry_limit, reason)
                    )
                    runtime_step = self._runtime_step(step)
                    result = await self._execute_runtime_step(runtime_step)
                    self.memory.commit(step, result)
                    await self._refresh_perception(runtime_step, result)
                    await self.events.emit(AgentEvent.step_done(step, result))
                    ok, reason = await self.verifier.check(step, result)
                    if not ok:
                        self._record_vision_failure(step, reason)
                        if self.state.vision_circuit_open:
                            retry_limit = retries
                    if not ok and reason.startswith("[ENV_ERR]"):
                        reason = reason.replace("[ENV_ERR] ", "")
                        retries = retry_limit
                        env_error = True

                if not ok:
                    # 用户取消 — 立即失败，不进入 replan
                    if env_error:
                        msg = f"Environment dependency/capability unavailable: {reason}"
                        logger.error(msg)
                        await self.events.emit(AgentEvent.error(msg))
                        task.mark_failed()
                        await self.events.emit(AgentEvent.task_done(
                            False, msg, self.state.step_count, 0))
                        return LoopResult(success=False, task=task, summary=msg)

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
                    new_steps, fallback, preserve_remaining = await self.planner.replan(
                        task, step, reason, context, remaining
                    )
                    if new_steps:
                        task.steps = self._merge_replan(
                            task.steps, step_index, new_steps, preserve_remaining
                        )
                        kept_count = len(remaining) if preserve_remaining else 0
                        logger.info(
                            f"Replan merged: {len(new_steps)} new + {kept_count} retained = "
                            f"{len(new_steps) + kept_count} total steps ahead"
                        )
                        continue
                    else:
                        task.mark_failed()
                        summary = fallback or f"Step failed: {reason}"
                        await self.events.emit(AgentEvent.task_done(
                            False, summary, self.state.step_count, 0))
                        return LoopResult(success=False, task=task, summary=summary)

            self.state.record_success()
            # replan_max limits consecutive recovery failures. A successful
            # action proves the new route made progress, so start a fresh count.
            replan_count = 0
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
