from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UIElement:
    """统一的 UI 元素描述，屏蔽 DOM / UIA 差异"""
    id: str = ""
    name: str = ""
    control_type: str = ""       # button | textbox | link | combobox | list_item | ...
    value: str = ""              # 当前文本/值
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # (left, top, right, bottom)
    enabled: bool = True
    visible: bool = True
    properties: dict[str, Any] = field(default_factory=dict)  # 平台特有属性
    children: list[UIElement] = field(default_factory=list)


@dataclass
class Selector:
    """跨平台元素定位器"""
    strategy: str                # css | xpath | uia_name | uia_automation_id | image
    value: str
    confidence: float = 1.0      # 图像匹配时使用
    fallback: Selector | None = None  # 主策略失效时的备用策略


@dataclass
class PerceptionResult:
    """统一的环境感知结果"""
    source: str                  # dom | uia_tree | screenshot | com
    summary: str = ""            # LLM 可读的文本描述
    raw: Any = None              # 原始数据
    elements: list[UIElement] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    error: str | None = None
