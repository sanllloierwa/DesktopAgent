from __future__ import annotations

import uuid
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class RetryPolicy(str, Enum):
    ONCE = "once"               # 失败不重试
    LINEAR = "linear"            # 固定间隔重试
    EXPONENTIAL = "exponential"  # 指数退避重试
    ADAPTIVE = "adaptive"       # 根据失败类型选择策略


@dataclass
class Step:
    """Planner 产出的单个执行步骤"""
    tool_name: str
    params: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    expected_outcome: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    retry_policy: RetryPolicy = RetryPolicy.ONCE
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ActionResult:
    """工具执行后返回的结构化结果"""
    step_id: str
    success: bool
    data: Any = None
    summary: str = ""
    screenshot_base64: str | None = None
    error: str | None = None
    duration_ms: float = 0.0


@dataclass
class Task:
    """一个完整的用户任务"""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    goal: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None

    @property
    def current_step_index(self) -> int:
        """已执行步骤数"""
        return sum(1 for s in self.steps if s.id)

    def mark_running(self) -> None:
        self.status = TaskStatus.RUNNING

    def mark_success(self) -> None:
        self.status = TaskStatus.SUCCESS
        self.completed_at = time.time()

    def mark_failed(self) -> None:
        self.status = TaskStatus.FAILED
        self.completed_at = time.time()
