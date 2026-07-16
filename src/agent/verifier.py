"Verifier — 判断步骤是否成功，用规则 + LLM 双重验证"

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from src.schemas.task import Step, ActionResult, Task
from src.agent.observer import Observer, Context


class Verifier:
    """步骤验证器。结合结构化规则和 LLM 视觉/文本分析来判定步骤成功与否。"""

    def __init__(self, observer: Observer, llm_client: Any = None) -> None:
        self.observer = observer
        self.llm = llm_client

    async def check(self, step: Step, result: ActionResult) -> tuple[bool, str]:
        """返回 (success, reason)"""
        # 1. 工具层已标记失败
        if not result.success:
            return False, result.error or "Tool execution returned failure"

        # 2. 快速规则检查
        rule_ok, rule_reason = self._rule_check(step, result)
        if not rule_ok:
            return False, rule_reason

        # 3. 如果是影响 UI 的操作，用 LLM 做进一步验证
        if self._needs_visual_check(step):
            ctx = await self.observer.gather()
            # 如果没有可用的感知上下文（无截图/DOM/UIA），信任工具结果
            if not ctx.screenshot_base64 and ctx.to_text() == "(no context available)":
                return True, "Step completed (no visual context available for LLM check)"
            return await self._llm_verify(step, result, ctx)

        return True, "Step completed successfully"

    async def check_task_completion(
        self,
        task: Task,
        completed_steps: list[tuple[Step, ActionResult]],
    ) -> tuple[bool, str]:
        """严格判断用户的最终目标是否已经由执行证据完成。"""
        if self.llm is None:
            return False, "No LLM available for final goal verification"

        evidence: list[str] = []
        for step, result in completed_steps[-20:]:
            status = "成功" if result.success else "失败"
            line = f"- [{status}] [{step.tool_name}] {step.description}: {result.summary}"
            if result.success and isinstance(result.data, dict):
                answer = result.data.get("answer")
                if isinstance(answer, str) and answer.strip():
                    line += f"；观察结果={answer[:500]}"
                confirmation = result.data.get("confirmation")
                if confirmation in {"yes", "no"}:
                    line += f"；人工确认={confirmation}"
                if result.data.get("foreground_verified") is True:
                    line += (
                        f"；前台目标已验证={result.data.get('target_app', '?')}"
                        f"({result.data.get('window_title', '?')})"
                    )
            evidence.append(line)

        prompt = f"""你是桌面 Agent 的最终目标审计器。请根据真实执行证据，严格判断用户目标是否已经完成。

用户目标: {task.goal}

执行证据:
{chr(10).join(evidence) if evidence else '(无执行证据)'}

判定规则:
- 只能依据成功步骤和观察结果，不能依据原计划或步骤描述中的意图自行推断成功。
- 截图、分析、打开应用、请求用户输入都只是中间步骤，不能证明发送、保存、发布等动作已完成。
- 对“发送消息”任务，必须有实际输入消息并提交发送的证据；仅粘贴文字不等于已发送。
- 全局键鼠工具只有在证据包含“前台目标已验证”时，才能证明操作发送给了目标应用。
- 人工确认=yes 可以证明对应确认步骤成立；人工确认=unknown 或仅有输入长度不能作为肯定证据。
- 如果证据不足，必须返回 success=false，并具体说明仍缺少什么动作。

只回答 JSON:
{{"success": true/false, "reason": "一句话原因"}}"""

        try:
            resp = await self.llm.messages.create(
                model=self.llm.model,
                max_tokens=200,
                temperature=0.0,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            if "```" in text:
                text = text.split("```", 2)[1]
                if text.startswith("json"):
                    text = text[4:]
            data = json.loads(text)
            return bool(data.get("success")), str(data.get("reason", "Final goal not verified"))
        except Exception as exc:
            logger.warning(f"Final goal verification failed: {exc}")
            return False, f"Final goal verification unavailable: {exc}"

    def _rule_check(self, step: Step, result: ActionResult) -> tuple[bool, str]:
        """基于规则的快速检查"""
        tool = step.tool_name

        # 检查是否有依赖的前置步骤已完成
        # （此处在 executor 层已有保证，这里是二次确认）

        # 结果中是否包含特定的错误标记
        if result.data and isinstance(result.data, dict):
            if result.data.get("error"):
                return False, str(result.data["error"])
            if "status_code" in result.data and result.data["status_code"] not in (200, 201, 204, 0):
                return False, f"HTTP {result.data['status_code']}"

        return True, ""

    def _needs_visual_check(self, step: Step) -> bool:
        """判断此步骤是否需要视觉验证。
        只有确实改变 UI 状态、且依赖截图才能判断的操作才需要 LLM 视觉验证。
        launch_app 工具自身已确认进程启动成功，无需额外视觉确认。
        """
        visual_tools = {
            "click", "navigate", "type_text", "focus_window",
            "desktop_keypress", "desktop_click", "desktop_move_mouse",
            "desktop_scroll", "desktop_drag",
        }
        return step.tool_name in visual_tools and self.llm is not None

    async def _llm_verify(
        self, step: Step, result: ActionResult, ctx: Context
    ) -> tuple[bool, str]:
        """用 LLM 判断步骤效果是否符合预期"""
        prompt = f"""你是桌面 Agent 的步骤验证器。判断以下步骤是否成功。

步骤描述: {step.description}
预期结果: {step.expected_outcome or '无明确预期'}
工具执行摘要: {result.summary}

当前环境状态:
{ctx.to_text()}

请回答 JSON:
{{"success": true/false, "reason": "一句话原因"}}"""

        try:
            resp = await self.llm.messages.create(
                model=self.llm.model,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
            data = json.loads(text)
            return data["success"], data["reason"]
        except Exception as exc:
            logger.warning(f"LLM verification failed, defaulting to rule result: {exc}")
            return True, "LLM verification skipped"
