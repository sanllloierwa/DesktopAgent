"Executor — 执行单个 Step，调用对应 Tool"

from __future__ import annotations

import time

from loguru import logger

from src.schemas.task import Step, ActionResult
from src.tools.base import ToolRegistry


class Executor:
    """步骤执行器。从 ToolRegistry 查找工具并执行，返回结构化结果。"""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def run(self, step: Step) -> ActionResult:
        logger.info(f"Executing step [{step.id}]: {step.description}")

        tool = self.registry.get(step.tool_name)
        if tool is None:
            return ActionResult(
                step_id=step.id,
                success=False,
                error=f"Tool '{step.tool_name}' not found in registry. "
                       f"Available: {self.registry.list_names()}",
            )

        start = time.perf_counter()
        result = await tool.safe_execute(**step.params)
        duration_ms = (time.perf_counter() - start) * 1000

        success = bool(result.get("success"))
        return ActionResult(
            step_id=step.id,
            success=success,
            data=result.get("data"),
            summary=result.get("summary", result.get("error", "")),
            screenshot_base64=result.get("screenshot_base64"),
            error=result.get("error") if not success else None,
            duration_ms=round(duration_ms, 1),
        )
