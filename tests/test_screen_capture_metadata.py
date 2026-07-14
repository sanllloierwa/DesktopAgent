from __future__ import annotations

from src.tools.desktop.screen_capture import DesktopScreenshotTool


async def test_desktop_screenshot_exposes_coordinate_metadata(monkeypatch) -> None:
    async def fake_capture():
        return {
            "success": True,
            "base64": "image-data",
            "size": (1920, 1080),
            "width": 1920,
            "height": 1080,
            "left": -1920,
            "top": 0,
            "right": 0,
            "bottom": 1080,
        }

    monkeypatch.setattr("src.perception.screenshot.capture_screenshot", fake_capture)

    result = await DesktopScreenshotTool().execute()

    assert result["success"] is True
    assert result["screenshot_base64"] == "image-data"
    assert result["width"] == 1920
    assert result["left"] == -1920
    assert result["right"] == 0
