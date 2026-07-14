from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from src.agent.loop import AgentLoop
from src.schemas.task import ActionResult
from src.tools.ai.vision import AnalyzeScreenTool
from src.tools.base import BaseTool, ToolSchema
from src.utils.config import load_config
from src.vision_mcp.agnes_client import exception_text


class _ScreenshotTool(BaseTool):
    schema = ToolSchema(name="shot", description="", parameters={})

    async def execute(self) -> dict:
        return {
            "success": True,
            "summary": "captured",
            "screenshot_base64": "image-data",
        }


async def test_screenshot_is_promoted_by_safe_execute() -> None:
    result = await _ScreenshotTool().safe_execute()
    assert result["screenshot_base64"] == "image-data"
    assert "screenshot_base64" not in result["data"]


def test_loop_can_resolve_screenshot_reference() -> None:
    loop = object.__new__(AgentLoop)
    result = ActionResult(
        step_id="1",
        success=True,
        screenshot_base64="image-data",
    )
    assert loop._lookup_in_result(result, "screenshot_base64") == "image-data"


async def test_analyze_screen_routes_through_mcp(monkeypatch) -> None:
    calls = []

    async def fake_mcp(image_base64, question, config):
        calls.append((image_base64, question, config.vision.transport))
        return {
            "answer": "Word 窗口已打开",
            "provider": "agnes",
            "model": "agnes-2.0-flash",
        }

    monkeypatch.setattr("src.tools.ai.vision.mcp_analyze_image", fake_mcp)
    result = await AnalyzeScreenTool().execute("image-data", "Word 是否打开？")

    assert result["success"] is True
    assert result["answer"] == "Word 窗口已打开"
    assert result["vision_provider"] == "agnes"
    assert result["vision_transport"] == "mcp"
    assert calls == [("image-data", "Word 是否打开？", "mcp")]


def test_main_and_vision_model_configs_are_independent() -> None:
    config = load_config()
    assert config.llm.provider == "deepseek"
    assert config.vision.provider == "agnes"
    assert config.vision.transport == "mcp"


def test_mcp_exception_group_is_flattened() -> None:
    error = ExceptionGroup(
        "task group failed",
        [RuntimeError("Error executing tool: Request timed out."), ValueError("stdio closed")],
    )

    assert exception_text(error) == "Error executing tool: Request timed out.; stdio closed"


def test_project_src_does_not_shadow_third_party_mcp_package() -> None:
    project_src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(project_src)!r}); "
        "import mcp; "
        "from mcp import ClientSession; "
        "print(mcp.__file__)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )

    imported_path = Path(result.stdout.strip()).resolve()
    assert project_src not in imported_path.parents
