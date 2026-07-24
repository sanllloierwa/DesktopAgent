"AgentState — 会话级可变状态容器"

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class AgentState:
    """持有当前会话的所有可变状态，Agent 循环中读写"""

    # 浏览器相关
    browser_launched: bool = False
    browser_page: Any = None
    last_browser_url: str = ""
    logged_in: dict[str, bool] = field(default_factory=dict)  # 各平台登录状态

    # 桌面相关
    active_window_handle: int | None = None
    app_processes: dict[str, Any] = field(default_factory=dict)  # app_name → Popen

    # 任务执行
    current_task_id: str = ""
    step_count: int = 0
    consecutive_failures: int = 0
    vision_timeout_failures: int = 0
    vision_circuit_open: bool = False

    # 上下文缓存
    last_screenshot_base64: str | None = None
    last_dom_summary: str = ""
    last_uia_summary: str = ""

    # 用户偏好（可在对话中学习）
    preferences: dict[str, Any] = field(default_factory=dict)

    def record_failure(self) -> None:
        self.consecutive_failures += 1

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.step_count += 1

    def record_vision_timeout(self, threshold: int = 2) -> None:
        self.vision_timeout_failures += 1
        if self.vision_timeout_failures >= threshold:
            self.vision_circuit_open = True

    def is_stuck(self, threshold: int = 5) -> bool:
        """连续失败超过阈值，判定为陷入困境"""
        return self.consecutive_failures >= threshold
