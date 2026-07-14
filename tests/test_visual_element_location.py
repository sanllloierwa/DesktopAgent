from __future__ import annotations

from src.tools.ai.vision import LocateScreenElementTool


async def test_visual_location_maps_image_point_to_physical_screen(monkeypatch) -> None:
    async def fake_mcp(image_base64, question, config):
        assert image_base64 == "image-data"
        assert "1920x1080" in question
        return {
            "answer": (
                '```json\n{"found": true, "bbox": [100, 200, 300, 260], '
                '"click_point": [240, 230], "confidence": 0.93, "reason": "唯一按钮"}\n```'
            ),
            "provider": "agnes",
            "model": "agnes-2.0-flash",
        }

    monkeypatch.setattr("src.tools.ai.vision.mcp_analyze_image", fake_mcp)

    result = await LocateScreenElementTool().execute(
        "image-data",
        "发送按钮",
        image_width=1920,
        image_height=1080,
        screen_left=-1920,
        screen_top=100,
    )

    assert result["success"] is True
    assert result["x"] == -1680
    assert result["y"] == 330
    assert result["bbox"] == [-1820, 300, -1620, 360]
    assert result["confidence"] == 0.93


async def test_visual_location_rejects_low_confidence(monkeypatch) -> None:
    async def fake_mcp(_image, _question, config):
        return {
            "answer": (
                '{"found": true, "bbox": [10, 10, 50, 50], '
                '"click_point": [30, 30], "confidence": 0.41, "reason": "多个候选"}'
            )
        }

    monkeypatch.setattr("src.tools.ai.vision.mcp_analyze_image", fake_mcp)

    result = await LocateScreenElementTool().execute(
        "image-data", "确定按钮", 100, 100, min_confidence=0.7
    )

    assert result["success"] is False
    assert "below required" in result["error"]


async def test_visual_location_rejects_out_of_bounds_coordinates(monkeypatch) -> None:
    async def fake_mcp(_image, _question, config):
        return {
            "answer": (
                '{"found": true, "bbox": [10, 10, 150, 50], '
                '"click_point": [30, 30], "confidence": 0.9, "reason": ""}'
            )
        }

    monkeypatch.setattr("src.tools.ai.vision.mcp_analyze_image", fake_mcp)

    result = await LocateScreenElementTool().execute(
        "image-data", "按钮", 100, 100
    )

    assert result["success"] is False
    assert "outside" in result["error"]
