"""将所有可用工具注册到 ToolRegistry"""

from __future__ import annotations

from loguru import logger

from src.tools.base import ToolRegistry

# Desktop tools
from src.tools.desktop.app_control import LaunchAppTool, CloseAppTool
from src.tools.desktop.wps_com import (
    CreateDocumentTool,
    WriteTextTool,
    SetFontTool,
    SetAlignmentTool,
    SaveDocumentTool,
    ExportPDFTool,
    InsertImageTool,
)

# Browser tools
from src.tools.browser.navigate import (
    NavigateTool,
    ClickTool,
    TypeTextTool as BrowserTypeTextTool,
    ScreenshotTool,
    GetDOMTool,
    ExtractTextTool,
)

# AI tools
from src.tools.ai.text_gen import GenerateArticleTool, SummarizeTool
from src.tools.ai.vision import AnalyzeScreenTool


def register_all_tools(registry: ToolRegistry) -> ToolRegistry:
    """将所有工具注册到给定的 registry 中，返回同一个 registry"""

    # --- Desktop ---
    registry.register(LaunchAppTool())
    registry.register(CloseAppTool())
    registry.register(CreateDocumentTool())
    registry.register(WriteTextTool())
    registry.register(SetFontTool())
    registry.register(SetAlignmentTool())
    registry.register(SaveDocumentTool())
    registry.register(ExportPDFTool())
    registry.register(InsertImageTool())

    # --- Browser ---
    registry.register(NavigateTool())
    registry.register(ClickTool())
    registry.register(BrowserTypeTextTool())
    registry.register(ScreenshotTool())
    registry.register(GetDOMTool())
    registry.register(ExtractTextTool())

    # --- AI ---
    registry.register(GenerateArticleTool())
    registry.register(SummarizeTool())
    registry.register(AnalyzeScreenTool())

    logger.info(f"Registered {len(registry.list_names())} tools: {registry.list_names()}")
    return registry
