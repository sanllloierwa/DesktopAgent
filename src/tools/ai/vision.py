"""Vision tool exposed to the DeepSeek-driven planner."""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from src.tools.base import BaseTool, ToolSchema
from src.vision_mcp.agnes_backend import analyze_image
from src.vision_mcp.agnes_client import exception_text, mcp_analyze_image
from src.utils.config import load_config


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if "```" in cleaned:
        parts = cleaned.split("```")
        cleaned = parts[1] if len(parts) > 1 else cleaned
        if cleaned.lstrip().startswith("json"):
            cleaned = cleaned.lstrip()[4:]
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Vision response did not contain a JSON object")
    value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Vision response JSON must be an object")
    return value


def _error_message(exc: Exception) -> str:
    err_msg = exception_text(exc)
    if (
        "401" in err_msg
        or "403" in err_msg
        or "无效的令牌" in err_msg
        or "invalid token" in err_msg.lower()
    ):
        return "[AUTH_ERR] " + err_msg
    return err_msg


async def _run_vision(image_base64: str, question: str) -> tuple[dict[str, Any], Any]:
    config = load_config()
    if config.vision.transport == "mcp":
        result = await mcp_analyze_image(image_base64, question, config=config)
    else:
        result = await analyze_image(image_base64, question, config=config)
    return result, config


class AnalyzeScreenTool(BaseTool):
    schema = ToolSchema(
        name="analyze_screen",
        description="分析截图内容，识别 UI 元素、文字、按钮等。需要传入 base64 编码的图片。",
        parameters={
            "type": "object",
            "properties": {
                "image_base64": {
                    "type": "string",
                    "description": "截图的 base64 编码",
                },
                "question": {
                    "type": "string",
                    "description": "要询问的问题，如 '发布按钮在哪里？' 或 '页面上的主要内容是什么？'",
                },
            },
            "required": ["image_base64", "question"],
        },
    )

    async def execute(self, image_base64: str, question: str = "描述这个画面中的内容") -> dict:
        try:
            result, config = await _run_vision(image_base64, question)
            answer = str(result.get("answer", ""))
            return {
                "success": True,
                "summary": f"Analysis: {answer[:200]}...",
                "answer": answer,
                "vision_provider": result.get("provider"),
                "vision_model": result.get("model"),
                "vision_transport": config.vision.transport,
            }
        except Exception as exc:
            err_msg = _error_message(exc)
            logger.error(f"Vision analysis failed: {err_msg}")
            return {"success": False, "error": err_msg}


class LocateScreenElementTool(BaseTool):
    schema = ToolSchema(
        name="locate_screen_element",
        description=(
            "在桌面截图中定位一个可见 UI 目标，返回物理屏幕坐标、边界框和置信度。"
            "需要使用 desktop_screenshot 返回的图片、尺寸和屏幕原点。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_base64": {"type": "string", "description": "截图 Base64"},
                "target": {
                    "type": "string",
                    "description": "要定位的唯一目标，如‘微信登录按钮’或‘发送按钮’",
                },
                "image_width": {"type": "integer", "description": "截图像素宽度"},
                "image_height": {"type": "integer", "description": "截图像素高度"},
                "screen_left": {
                    "type": "integer",
                    "description": "截图左上角对应的物理屏幕 X，使用 desktop_screenshot.left",
                },
                "screen_top": {
                    "type": "integer",
                    "description": "截图左上角对应的物理屏幕 Y，使用 desktop_screenshot.top",
                },
                "min_confidence": {
                    "type": "number",
                    "description": "最低接受置信度，默认 0.70",
                },
            },
            "required": ["image_base64", "target", "image_width", "image_height"],
        },
    )

    async def execute(
        self,
        image_base64: str,
        target: str,
        image_width: int,
        image_height: int,
        screen_left: int = 0,
        screen_top: int = 0,
        min_confidence: float = 0.70,
    ) -> dict:
        if image_width <= 0 or image_height <= 0:
            return {"success": False, "error": "image_width and image_height must be positive"}
        threshold = max(0.0, min(float(min_confidence), 1.0))
        question = f"""在这张 {image_width}x{image_height} 像素的桌面截图中定位目标：{target}

只返回一个 JSON 对象，不要使用 Markdown，不要解释：
{{"found": true/false, "bbox": [left, top, right, bottom], "click_point": [x, y], "confidence": 0.0, "reason": "简短说明"}}

规则：
- 所有坐标都必须是相对于当前图片左上角的整数像素坐标；
- bbox 必须包围目标可点击区域，click_point 必须位于 bbox 内部；
- 如果目标不可见、被遮挡、存在多个无法区分的候选，found=false；
- 不要猜测不可见目标。"""
        try:
            result, config = await _run_vision(image_base64, question)
            payload = _parse_json_object(str(result.get("answer", "")))
            if payload.get("found") is not True:
                return {
                    "success": False,
                    "error": f"Target not found: {payload.get('reason', target)}",
                }

            bbox = payload.get("bbox")
            point = payload.get("click_point")
            confidence = float(payload.get("confidence", 0.0))
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValueError("Vision bbox must contain four coordinates")
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("Vision click_point must contain two coordinates")

            left, top, right, bottom = [int(round(float(value))) for value in bbox]
            rel_x, rel_y = [int(round(float(value))) for value in point]
            if not (0 <= left < right <= image_width and 0 <= top < bottom <= image_height):
                raise ValueError(f"Vision bbox is outside the {image_width}x{image_height} image")
            if not (left <= rel_x <= right and top <= rel_y <= bottom):
                raise ValueError("Vision click_point is outside its bbox")
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("Vision confidence must be between 0 and 1")
            if confidence < threshold:
                return {
                    "success": False,
                    "error": (
                        f"Target confidence {confidence:.2f} is below required "
                        f"{threshold:.2f}: {payload.get('reason', target)}"
                    ),
                }

            absolute_bbox = [
                screen_left + left,
                screen_top + top,
                screen_left + right,
                screen_top + bottom,
            ]
            return {
                "success": True,
                "summary": (
                    f"Located '{target}' at ({screen_left + rel_x}, {screen_top + rel_y}) "
                    f"with confidence {confidence:.2f}"
                ),
                "target": target,
                "x": screen_left + rel_x,
                "y": screen_top + rel_y,
                "relative_x": rel_x,
                "relative_y": rel_y,
                "bbox": absolute_bbox,
                "relative_bbox": [left, top, right, bottom],
                "confidence": confidence,
                "reason": str(payload.get("reason", "")),
                "vision_provider": result.get("provider"),
                "vision_model": result.get("model"),
                "vision_transport": config.vision.transport,
            }
        except Exception as exc:
            err_msg = _error_message(exc)
            logger.error(f"Screen element location failed: {err_msg}")
            return {"success": False, "error": err_msg}
