from __future__ import annotations

import sys
from types import SimpleNamespace

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


async def test_wechat_screenshot_is_cropped_to_verified_window(
    monkeypatch,
) -> None:
    captured_regions: list[tuple[int, int, int, int]] = []

    async def fake_capture(region):
        captured_regions.append(region)
        left, top, right, bottom = region
        return {
            "success": True,
            "base64": "wechat-image",
            "size": (right - left, bottom - top),
            "width": right - left,
            "height": bottom - top,
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
        }

    monkeypatch.setattr(
        "src.tools.desktop.native_control.activate_window",
        lambda _app: (321, "微信"),
    )
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(GetWindowRect=lambda _hwnd: (-1600, 40, -600, 840)),
    )
    monkeypatch.setattr(
        "src.perception.screenshot.capture_screenshot",
        fake_capture,
    )

    result = await DesktopScreenshotTool().execute(app_name="wechat")

    assert result["success"] is True
    assert captured_regions == [(-1600, 40, -600, 840)]
    assert result["target_app"] == "wechat"
    assert result["window_handle"] == 321
    assert result["foreground_verified"] is True
