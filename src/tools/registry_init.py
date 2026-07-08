"""将所有可用工具注册到 ToolRegistry"""

from __future__ import annotations

from loguru import logger

from src.tools.base import ToolRegistry

# Desktop tools
from src.tools.desktop.app_control import LaunchAppTool, CloseAppTool
from src.tools.desktop.screen_capture import DesktopScreenshotTool
from src.tools.desktop.text_input import DesktopTypeTextTool
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
    CheckLoginStatusTool,
)

# AI tools
from src.tools.ai.text_gen import GenerateArticleTool, SummarizeTool
from src.tools.ai.vision import AnalyzeScreenTool

# Interactive tools
from src.tools.interactive.user_input import RequestUserInputTool


def register_all_tools(registry: ToolRegistry) -> ToolRegistry:
    """将所有工具注册到给定的 registry 中，返回同一个 registry"""

    # 动态发现已安装应用
    try:
        from src.utils.app_discovery import discover_apps
        discovered_apps = discover_apps()
    except Exception as exc:
        logger.warning(f"App discovery failed, using empty list: {exc}")
        discovered_apps = {}

    # --- Desktop ---
    registry.register(LaunchAppTool(discovered_apps=discovered_apps))
    registry.register(CloseAppTool(discovered_apps=discovered_apps))
    registry.register(DesktopScreenshotTool())
    registry.register(DesktopTypeTextTool())
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
    registry.register(CheckLoginStatusTool())

    # --- AI ---
    registry.register(GenerateArticleTool())
    registry.register(SummarizeTool())
    registry.register(AnalyzeScreenTool())

    # --- Interactive ---
    registry.register(RequestUserInputTool())

    logger.info(f"Registered {len(registry.list_names())} tools: {registry.list_names()}")
    return registry
