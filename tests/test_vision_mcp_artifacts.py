from __future__ import annotations

import base64
import json
import zipfile

from src.utils.config import AppConfig
from src.vision_mcp.artifacts import write_mcp_artifact


def _config(tmp_path, *, retention: int = 50) -> AppConfig:
    return AppConfig.model_validate({
        "vision": {
            "artifact_output_enabled": True,
            "artifact_dir": str(tmp_path),
            "artifact_retention": retention,
        },
    })


def test_artifact_contains_request_response_and_screenshot(tmp_path) -> None:
    image = b"fake-png-bytes"
    path = write_mcp_artifact(
        tool_name="analyze_image_with_agnes",
        arguments={
            "image_base64": base64.b64encode(image).decode("ascii"),
            "question": "微信是否已登录？",
            "media_type": "image/png",
        },
        config=_config(tmp_path),
        response={"answer": "已登录"},
        duration_ms=12.5,
    )

    assert path is not None
    assert path.parent == tmp_path
    with zipfile.ZipFile(path) as archive:
        assert set(archive.namelist()) == {
            "manifest.json",
            "request.json",
            "response.json",
            "screenshot.png",
        }
        request = json.loads(archive.read("request.json"))
        response = json.loads(archive.read("response.json"))
        manifest = json.loads(archive.read("manifest.json"))
        assert request["question"] == "微信是否已登录？"
        assert request["image_base64"]["stored_separately"] is True
        assert response == {"answer": "已登录"}
        assert archive.read("screenshot.png") == image
        assert manifest["success"] is True
        assert manifest["screenshot_entry"] == "screenshot.png"


def test_error_artifact_and_retention(tmp_path) -> None:
    config = _config(tmp_path, retention=2)
    for index in range(3):
        path = write_mcp_artifact(
            tool_name="analyze_image_with_agnes",
            arguments={"question": f"question-{index}"},
            config=config,
            error="Request timed out",
        )
        assert path is not None

    archives = list(tmp_path.glob("*.zip"))
    assert len(archives) == 2
    with zipfile.ZipFile(archives[-1]) as archive:
        assert "error.json" in archive.namelist()


def test_artifact_output_can_be_disabled(tmp_path) -> None:
    config = AppConfig.model_validate({
        "vision": {
            "artifact_output_enabled": False,
            "artifact_dir": str(tmp_path),
        },
    })

    assert write_mcp_artifact(
        tool_name="vision_health",
        arguments={},
        config=config,
        response={"ok": True},
    ) is None
    assert list(tmp_path.iterdir()) == []
