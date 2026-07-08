"""Pre-step guard rules for dynamic pages.

The guard performs cheap deterministic checks before a planned step runs. It
keeps obvious stale-plan cases out of the executor and only asks the planner to
replan when the current page contradicts the next step.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from src.agent.executor import Executor
from src.agent.memory import MemoryHub
from src.schemas.task import ActionResult, Step, Task


class GuardDecision(str, Enum):
    CONTINUE = "continue"
    SKIP = "skip"
    REPLAN = "replan"
    COMPLETE = "complete"


@dataclass
class GuardResult:
    decision: GuardDecision = GuardDecision.CONTINUE
    reason: str = ""
    action_result: ActionResult | None = None


class StepGuard:
    """Rule-based guard that checks whether the next planned step still fits."""

    def __init__(self, executor: Executor, memory: MemoryHub) -> None:
        self.executor = executor
        self.memory = memory

    async def before_step(self, task: Task, step: Step) -> GuardResult:
        if (
            self._is_login_task(task)
            and self._has_navigated_to_page()
            and not self._has_checked_login_status()
        ):
            platform = self._platform_for_goal(task.goal)
            check = Step(
                tool_name="check_login_status",
                params={"platform": platform},
                description=f"检查 {platform} 是否已登录",
                expected_outcome="如果已登录则无需继续登录流程",
            )
            result = await self.executor.run(check)
            self.memory.commit(check, result)
            if result.success and isinstance(result.data, dict):
                if result.data.get("task_complete") or result.data.get("logged_in"):
                    return GuardResult(
                        decision=GuardDecision.COMPLETE,
                        reason=result.summary or "Already logged in",
                        action_result=result,
                    )
            return GuardResult(decision=GuardDecision.CONTINUE, action_result=result)

        if self._is_password_step(step) and self._recent_dom_says_verification_login():
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason="计划要输入密码，但当前 DOM 显示为验证码登录或未发现密码输入框",
            )

        if self._is_sms_code_input_step(step) and not self._sms_code_was_sent():
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason="计划要输入短信验证码，但还没有成功发送验证码",
            )

        if self._is_login_submit_step(step) and self._recent_dom_says_agreement_needed():
            return GuardResult(
                decision=GuardDecision.REPLAN,
                reason="登录提交前可能需要先勾选同意协议/隐私政策",
            )

        return GuardResult()

    def _is_login_task(self, task: Task) -> bool:
        goal = task.goal.lower()
        return any(word in goal for word in ("登录", "登陆", "login", "sign in"))

    def _platform_for_goal(self, goal: str) -> str:
        if "知乎" in goal or "zhihu" in goal.lower():
            return "zhihu"
        return "generic"

    def _has_checked_login_status(self) -> bool:
        return any(step.tool_name == "check_login_status" for step, _ in self.memory.working.completed_steps)

    def _has_navigated_to_page(self) -> bool:
        return any(
            step.tool_name == "navigate" and result.success
            for step, result in self.memory.working.completed_steps
        )

    def _recent_dom(self) -> str:
        for step, result in reversed(self.memory.working.completed_steps):
            if step.tool_name == "get_dom" and result.success and result.summary:
                return result.summary
        return ""

    def _recent_text(self) -> str:
        parts: list[str] = []
        for step, result in self.memory.working.completed_steps[-8:]:
            parts.append(step.description)
            parts.append(result.summary or "")
            if isinstance(result.data, dict):
                summary = result.data.get("summary")
                if isinstance(summary, str):
                    parts.append(summary)
        return "\n".join(parts)

    def _is_password_step(self, step: Step) -> bool:
        text = f"{step.description} {step.params}".lower()
        return "密码" in text or "password" in text

    def _recent_dom_says_verification_login(self) -> bool:
        dom = self._recent_dom()
        if not dom:
            return False
        has_password = "password" in dom.lower() or "密码" in dom
        has_verification = any(word in dom for word in ("验证码登录", "获取验证码", "短信验证码", "验证码"))
        return has_verification and not has_password

    def _is_sms_code_input_step(self, step: Step) -> bool:
        text = f"{step.description} {step.params}"
        return "验证码" in text and step.tool_name in {"type_text", "desktop_type_text"}

    def _sms_code_was_sent(self) -> bool:
        sent_words = ("发送短信验证码", "发送验证码", "获取验证码")
        for step, result in reversed(self.memory.working.completed_steps):
            text = f"{step.description}\n{result.summary}"
            if step.tool_name == "click" and result.success and any(word in text for word in sent_words):
                return True
        return False

    def _is_login_submit_step(self, step: Step) -> bool:
        text = f"{step.description} {step.params}"
        return step.tool_name == "click" and "登录" in text

    def _recent_dom_says_agreement_needed(self) -> bool:
        dom = self._recent_dom()
        if not dom:
            return False
        has_agreement = any(word in dom for word in ("同意", "协议", "隐私", "服务条款"))
        has_unchecked = any(word in dom.lower() for word in ('type="checkbox"', 'aria-checked="false"', 'checked="false"'))
        return has_agreement and has_unchecked
