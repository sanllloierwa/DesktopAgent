"Planner — 将自然语言目标分解为可执行步骤序列"

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from src.schemas.task import Task, Step, RetryPolicy
from src.tools.base import ToolRegistry


class Planner:
    """任务规划器：使用 LLM 将用户的高层目标分解为有序工具调用序列。"""

    SYSTEM_PROMPT = """你是桌面 Agent 的任务规划器。根据用户目标和可用工具，输出 JSON 格式的执行步骤序列。

规则:
1. 每个步骤必须使用可用工具列表中的某个工具名
2. 参数必须从用户目标中推导，不能臆造
3. 步骤顺序必须符合操作逻辑（如"点击发布"必须在"写文章"之后）
4. 失败重试策略默认使用 "once"，仅对网络相关操作使用 "exponential"
5. 只在确定需要重试时使用 retry_policy，大多数步骤保持 "once"

输出格式:
{"steps": [{"tool_name": "...", "params": {...}, "description": "...", "expected_outcome": "...", "retry_policy": "once"}]}"""

    def __init__(self, llm_client: Any, registry: ToolRegistry) -> None:
        self.llm = llm_client
        self.registry = registry
        self.model: str = getattr(llm_client, "model", "claude-sonnet-4-6")

    def _tool_descriptions(self) -> str:
        lines: list[str] = []
        for name in self.registry.list_names():
            tool = self.registry.get(name)
            if tool:
                lines.append(f"- {name}: {tool.schema.description}")
                params = tool.schema.parameters
                if params and "properties" in params:
                    for pname, pinfo in params["properties"].items():
                        required = pname in params.get("required", [])
                        req_mark = "*" if required else ""
                        lines.append(f"    {pname}{req_mark}: {pinfo.get('description', '')}")
        return "\n".join(lines)

    async def plan(self, task: Task, context: str = "") -> list[Step]:
        """根据任务目标规划执行步骤"""
        tools_text = self._tool_descriptions()

        user_prompt = f"""用户目标: {task.goal}

当前上下文:
{context or '(无)'}

可用工具:
{tools_text}

请规划执行步骤 (JSON):"""

        try:
            data = await self._call_llm(user_prompt)
            steps = self._parse_steps(data)
            logger.info(f"Planner generated {len(steps)} steps for task '{task.goal[:50]}...'")
            return steps
        except Exception as exc:
            logger.error(f"Planning failed: {exc}")
            return []

    async def replan(self, task: Task, failed_step: Step, error_reason: str, context: str = "") -> list[Step]:
        """在某个步骤失败后重新规划后续步骤"""
        tools_text = self._tool_descriptions()

        user_prompt = f"""用户目标: {task.goal}

一个步骤执行失败了，请重新规划后续步骤。
失败步骤: {failed_step.description}
失败原因: {error_reason}

当前上下文:
{context or '(无)'}

可用工具:
{tools_text}

请规划接下来的执行步骤 (JSON):"""

        try:
            data = await self._call_llm(user_prompt)
            steps = self._parse_steps(data)
            logger.info(f"Replan generated {len(steps)} alternative steps")
            return steps
        except Exception as exc:
            logger.error(f"Replan failed: {exc}")
            return []

    async def _call_llm(self, user_prompt: str) -> dict[str, Any]:
        """调用 LLM，返回解析后的 JSON"""
        resp = await self.llm.messages.create(
            model=self.model,
            max_tokens=2000,
            temperature=0.2,
            system=self.SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = resp.content[0].text
        # 处理可能的 markdown code block 包裹
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)

    def _parse_steps(self, data: dict[str, Any]) -> list[Step]:
        """将 LLM 输出转为 Step 对象列表"""
        raw_steps: list[dict] = data.get("steps", [])
        steps: list[Step] = []
        for rs in raw_steps:
            retry = RetryPolicy.ONCE
            if rs.get("retry_policy") in {"linear", "exponential", "adaptive"}:
                retry = RetryPolicy(rs["retry_policy"])
            steps.append(Step(
                tool_name=rs["tool_name"],
                params=rs.get("params", {}),
                description=rs.get("description", ""),
                expected_outcome=rs.get("expected_outcome", ""),
                retry_policy=retry,
            ))
        return steps
