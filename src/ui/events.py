"""Agent 执行事件系统 — 解耦 Agent 核心与 UI 层"""

from __future__ import annotations

import time
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable

from src.schemas.task import Step, ActionResult


class EventType(str, Enum):
    PLAN_START = "plan_start"
    PLAN_DONE = "plan_done"            # 规划完成，携带步骤列表
    STEP_START = "step_start"          # 开始执行某一步
    STEP_DONE = "step_done"            # 步骤执行完成
    STEP_RETRY = "step_retry"          # 正在重试
    SCREENSHOT = "screenshot"          # 新的截图可用
    ERROR = "error"                    # 发生错误
    TASK_DONE = "task_done"            # 任务完成
    LOG = "log"                        # 普通日志消息


@dataclass
class AgentEvent:
    type: EventType
    timestamp: float = field(default_factory=time.time)
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def plan_start(cls, goal: str) -> "AgentEvent":
        return cls(type=EventType.PLAN_START, message=f"Planning: {goal[:80]}...", data={"goal": goal})

    @classmethod
    def plan_done(cls, steps: list[Step]) -> "AgentEvent":
        return cls(
            type=EventType.PLAN_DONE,
            message=f"Plan ready: {len(steps)} steps",
            data={"steps": [{"tool": s.tool_name, "desc": s.description, "id": s.id} for s in steps]},
        )

    @classmethod
    def step_start(cls, step: Step, index: int, total: int) -> "AgentEvent":
        return cls(
            type=EventType.STEP_START,
            message=f"[{index}/{total}] {step.description}",
            data={"step_id": step.id, "tool": step.tool_name, "index": index, "total": total},
        )

    @classmethod
    def step_done(cls, step: Step, result: ActionResult) -> "AgentEvent":
        msg = f"{'OK' if result.success else 'FAIL'}: {step.description}"
        if not result.success and result.error:
            # 截断过长错误信息，保留关键内容
            err = result.error[:120] + "..." if len(result.error or "") > 120 else result.error
            msg += f" — {err}"
        return cls(
            type=EventType.STEP_DONE,
            message=msg,
            data={
                "step_id": step.id,
                "success": result.success,
                "summary": result.summary,
                "error": result.error,
                "duration_ms": result.duration_ms,
                "screenshot_base64": result.screenshot_base64,
            },
        )

    @classmethod
    def step_retry(cls, step: Step, attempt: int, max_retries: int, reason: str) -> "AgentEvent":
        return cls(
            type=EventType.STEP_RETRY,
            message=f"Retry {attempt}/{max_retries} for: {step.description}",
            data={"step_id": step.id, "attempt": attempt, "max_retries": max_retries, "reason": reason},
        )

    @classmethod
    def screenshot(cls, base64_data: str, label: str = "") -> "AgentEvent":
        return cls(
            type=EventType.SCREENSHOT,
            message=f"Screenshot: {label}" if label else "Screenshot captured",
            data={"screenshot_base64": base64_data, "label": label},
        )

    @classmethod
    def error(cls, message: str, detail: dict | None = None) -> "AgentEvent":
        return cls(type=EventType.ERROR, message=message, data=detail or {})

    @classmethod
    def task_done(cls, success: bool, summary: str, total_steps: int, duration_ms: float) -> "AgentEvent":
        return cls(
            type=EventType.TASK_DONE,
            message=summary,
            data={"success": success, "total_steps": total_steps, "duration_ms": duration_ms},
        )

    @classmethod
    def log(cls, message: str) -> "AgentEvent":
        return cls(type=EventType.LOG, message=message)


# 事件处理器类型
EventHandler = Callable[[AgentEvent], Awaitable[None]]


class EventBus:
    """异步事件总线：Agent 发布事件，UI 订阅事件"""

    def __init__(self) -> None:
        self._handlers: list[EventHandler] = []
        self._history: list[AgentEvent] = []  # 保留历史供 UI 初始化

    def subscribe(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        if handler in self._handlers:
            self._handlers.remove(handler)

    async def emit(self, event: AgentEvent) -> None:
        self._history.append(event)
        for handler in self._handlers:
            try:
                await handler(event)
            except Exception:
                pass  # 单个 handler 失败不影响其他

    def replay(self, since: float = 0.0) -> list[AgentEvent]:
        """回放历史事件（用于 UI 初始化/重连时同步状态）"""
        return [e for e in self._history if e.timestamp >= since]

    def clear_history(self) -> None:
        self._history.clear()
