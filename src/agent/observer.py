"Observer — 聚合多种感知源，生成统一的环境上下文"

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.schemas.element import PerceptionResult
from src.agent.state import AgentState


@dataclass
class Context:
    """Observer 聚合后的环境上下文，供 Planner 和 Verifier 使用"""
    dom_summary: str = ""
    uia_summary: str = ""
    screenshot_base64: str | None = None
    active_elements: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        parts: list[str] = []
        if self.dom_summary:
            parts.append(f"[DOM]\n{self.dom_summary}")
        if self.uia_summary:
            parts.append(f"[UI Automation]\n{self.uia_summary}")
        if self.active_elements:
            elements_text = "\n".join(
                f"  - {e.get('name', '?')} [{e.get('control_type', '?')}]"
                for e in self.active_elements[:15]
            )
            parts.append(f"[Interactive Elements]\n{elements_text}")
        if self.extra:
            parts.append(f"[Extra]\n{self.extra}")
        return "\n\n".join(parts) if parts else "(no context available)"


class Observer:
    """环境观察器。从浏览器、桌面等感知源收集信息，生成统一 Context。"""

    def __init__(self, state: AgentState) -> None:
        self.state = state

    async def gather(self) -> Context:
        ctx = Context()

        if self.state.last_dom_summary:
            ctx.dom_summary = self.state.last_dom_summary
        if self.state.last_uia_summary:
            ctx.uia_summary = self.state.last_uia_summary
        if self.state.last_screenshot_base64:
            ctx.screenshot_base64 = self.state.last_screenshot_base64

        return ctx

    def feed_perception(self, result: PerceptionResult) -> None:
        """将感知层结果注入 AgentState"""
        if result.source == "dom":
            self.state.last_dom_summary = result.summary
        elif result.source == "uia_tree":
            self.state.last_uia_summary = result.summary
        elif result.source == "screenshot":
            self.state.last_screenshot_base64 = result.summary
