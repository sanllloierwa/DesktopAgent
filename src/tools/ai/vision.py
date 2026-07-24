"""Vision tool exposed to the DeepSeek-driven planner."""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
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
    lowered = err_msg.lower()
    if "request timed out" in lowered or "vision mcp timed out" in lowered:
        return "[VISION_TIMEOUT] " + err_msg
    return err_msg


def _artifact_output(source: Any) -> dict[str, str]:
    if isinstance(source, dict):
        path = source.get("mcp_artifact_path")
    else:
        path = getattr(source, "mcp_artifact_path", None)
    return {"mcp_artifact_path": str(path)} if path else {}


def _normalized_visible_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"[\s\u200b\u200e\u200f]+", "", normalized).strip(
        "\"'“”‘’「」『』"
    )


async def _run_vision(
    image_base64: str,
    question: str,
    *,
    json_mode: bool = False,
) -> tuple[dict[str, Any], Any]:
    config = load_config()
    if config.vision.transport == "mcp":
        if json_mode:
            result = await mcp_analyze_image(
                image_base64, question, config=config, json_mode=True
            )
        else:
            result = await mcp_analyze_image(image_base64, question, config=config)
    else:
        if json_mode:
            result = await analyze_image(
                image_base64, question, config=config, json_mode=True
            )
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
                **_artifact_output(result),
            }
        except Exception as exc:
            err_msg = _error_message(exc)
            logger.error(f"Vision analysis failed: {err_msg}")
            return {"success": False, "error": err_msg, **_artifact_output(exc)}


class CheckWeChatLoginStatusTool(BaseTool):
    """Return a conservative structured login decision for the WeChat client."""

    schema = ToolSchema(
        name="check_wechat_login_status",
        description=(
            "根据微信窗口截图返回结构化登录状态。只有明确看到微信主界面时"
            " logged_in 才为 true；扫码、进入微信、切换账号或不确定均为 false。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_base64": {
                    "type": "string",
                    "description": "desktop_screenshot 返回的微信窗口截图",
                },
            },
            "required": ["image_base64"],
        },
    )

    async def execute(self, image_base64: str) -> dict:
        question = """判断这张截图中的微信客户端是否已经登录并进入可操作的主界面。

只返回一个 JSON 对象，不要使用 Markdown：
{"logged_in": true/false, "state": "main_ui/login_required/uncertain", "reason": "简短依据"}

判定规则：
- 只有明确看到微信主界面（左侧导航、会话列表或联系人列表、顶部搜索入口）时，logged_in=true、state="main_ui"；
- 看到二维码、扫码登录、进入微信、切换账号、头像登录确认等登录/进入界面时，logged_in=false、state="login_required"；
- 画面不完整、被遮挡或无法确定时，logged_in=false、state="uncertain"；
- 不要因为窗口标题是“微信”就判定已经登录。"""
        try:
            result, config = await _run_vision(
                image_base64,
                question,
                json_mode=True,
            )
            payload = _parse_json_object(str(result.get("answer", "")))
            raw_logged_in = payload.get("logged_in")
            logged_in = raw_logged_in is True
            state = str(payload.get("state", "")).strip().lower()
            if logged_in:
                state = "main_ui"
            elif state not in {"login_required", "uncertain"}:
                state = "uncertain"
            reason = str(payload.get("reason", "")).strip()
            return {
                "success": True,
                "summary": (
                    "WeChat login status: "
                    f"{'logged in' if logged_in else state}"
                    + (f" ({reason})" if reason else "")
                ),
                "logged_in": logged_in,
                "login_required": state == "login_required",
                "state": state,
                "reason": reason,
                "vision_provider": result.get("provider"),
                "vision_model": result.get("model"),
                "vision_transport": config.vision.transport,
                **_artifact_output(result),
            }
        except Exception as exc:
            err_msg = _error_message(exc)
            logger.error(f"WeChat login status analysis failed: {err_msg}")
            return {
                "success": False,
                "error": err_msg,
                **_artifact_output(exc),
            }


class OpenWeChatSearchCandidateTool(BaseTool):
    """Atomically locate an exact-name WeChat candidate and click it."""

    schema = ToolSchema(
        name="open_wechat_search_candidate",
        description=(
            "在微信搜索建议或结果截图中按显示名称定位候选项并立即点击。"
            "兼容新版搜一搜“账号”和旧版“公众号”筛选；候选层无需"
            "预先显示服务号标签，进入资料页后再验证类型。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "image_base64": {"type": "string", "description": "微信窗口截图"},
                "target_name": {
                    "type": "string",
                    "description": "需要打开的精确显示名称，如火眼审阅",
                },
                "expected_type": {
                    "type": "string",
                    "enum": ["service_account", "public_account", "any"],
                    "description": "期望账号类型；服务号任务使用 service_account",
                },
                "image_width": {"type": "integer", "description": "截图宽度"},
                "image_height": {"type": "integer", "description": "截图高度"},
                "screen_left": {"type": "integer", "description": "截图屏幕左坐标"},
                "screen_top": {"type": "integer", "description": "截图屏幕上坐标"},
                "min_confidence": {
                    "type": "number",
                    "description": "最低定位置信度，默认0.70",
                },
            },
            "required": [
                "image_base64",
                "target_name",
                "image_width",
                "image_height",
            ],
        },
    )

    async def execute(
        self,
        image_base64: str,
        target_name: str,
        image_width: int,
        image_height: int,
        screen_left: int = 0,
        screen_top: int = 0,
        min_confidence: float = 0.70,
        expected_type: str = "service_account",
    ) -> dict:
        target_name = target_name.strip()
        if not target_name:
            return {"success": False, "error": "target_name must not be empty"}
        if expected_type not in {"service_account", "public_account", "any"}:
            return {
                "success": False,
                "error": f"Unsupported expected_type: {expected_type}",
            }
        locator = LocateScreenElementTool()
        located = await locator.execute(
            image_base64=image_base64,
            target=(
                f'微信搜索建议或搜索结果中，主要显示名称与“{target_name}”'
                "完全匹配的可点击候选项。不要要求候选项同时显示服务号或"
                "公众号标签；忽略仅部分匹配或名称不同的项。"
            ),
            image_width=image_width,
            image_height=image_height,
            screen_left=screen_left,
            screen_top=screen_top,
            min_confidence=min_confidence,
            expected_text=target_name,
            require_exact_text=True,
        )
        if not located.get("success"):
            return {
                "success": False,
                "error": located.get(
                    "error",
                    f"Cannot locate exact WeChat candidate: {target_name}",
                ),
                **_artifact_output(located),
            }

        from src.tools.desktop.native_control import DesktopClickTool

        clicked = await DesktopClickTool().execute(
            x=int(located["x"]),
            y=int(located["y"]),
            confidence=float(located.get("confidence", 1.0)),
            min_confidence=min_confidence,
            app_name="wechat",
            wait_after=0.8,
        )
        if not clicked.get("success"):
            return {
                "success": False,
                "error": clicked.get("error", "WeChat candidate click failed"),
            }

        from src.tools.desktop.screen_capture import DesktopScreenshotTool

        last_artifact = _artifact_output(located)
        navigation_actions = 1  # the initial exact-name candidate click
        observations = 0
        while observations < 8 and navigation_actions <= 4:
            observations += 1
            screen = await DesktopScreenshotTool().execute(app_name="wechat")
            if not screen.get("success"):
                return {
                    "success": False,
                    "error": screen.get(
                        "error",
                        "Could not verify WeChat page after candidate click",
                    ),
                }
            state_result, config = await _run_vision(
                screen["screenshot_base64"],
                f"""判断点击“{target_name}”后当前微信页面处于哪种状态。

只返回 JSON：
{{"state": "account_profile/search_page/search_results/loading/other", "target_visible": true/false,
  "account_type": "service_account/public_account/personal/unknown",
  "selected_tab": "accounts/official_accounts/mini_programs/other/unknown",
  "available_tabs": ["账号", "公众号", "小程序"],
  "search_query": "搜索框当前完整文字；不可见则为空字符串",
  "reason": "简短依据"}}

规则：
- account_profile：已经进入名称为“{target_name}”的账号资料/主页，可看到关注或发消息等资料页操作；
- search_page：已进入搜一搜页面，只看到搜索框/搜索按钮且结果区为空，需要先执行页面内搜索；
- search_results：仍是搜一搜、搜索建议或搜索结果页面（即使结果区暂时为空），需要切换筛选或继续点击；
- loading：页面只有加载动画、骨架屏或短暂空白，筛选栏和稳定的搜索按钮均尚未出现；
- other：聊天主页、空白页、登录页或无法确定；
- 当前选中新版“账号”时 selected_tab=accounts；选中旧版“公众号”时
  selected_tab=official_accounts；选中“小程序”时 selected_tab=mini_programs；
- “账号”分类可能同时包含服务号、公众号和个人账号，必须进入资料页后核验类型；
- search_query 必须逐字抄录搜索框中的当前关键词，不得省略“下载”等后缀；
- “服务号”对应 service_account；无法看到类型时用 unknown。""",
                json_mode=True,
            )
            payload = _parse_json_object(str(state_result.get("answer", "")))
            state = str(payload.get("state", "other")).strip().lower()
            target_visible = payload.get("target_visible") is True
            account_type = str(
                payload.get("account_type", "unknown")
            ).strip().lower()
            selected_tab = str(
                payload.get("selected_tab", "unknown")
            ).strip().lower()
            available_tabs = payload.get("available_tabs")
            if not isinstance(available_tabs, list):
                available_tabs = []
            search_query = str(payload.get("search_query", "")).strip()
            if (
                state == "other"
                and (
                    selected_tab in {
                        "accounts",
                        "official_accounts",
                        "mini_programs",
                    }
                    or any(
                        str(tab).strip() in {"账号", "公众号", "小程序"}
                        for tab in available_tabs
                    )
                )
            ):
                state = "search_results"
            last_artifact = _artifact_output(state_result) or last_artifact

            if (
                state in {"search_page", "search_results"}
                and search_query
                and _normalized_visible_text(search_query)
                != _normalized_visible_text(target_name)
            ):
                return {
                    "success": False,
                    "error": (
                        "[SEARCH_QUERY_DRIFT] Refusing further clicks: expected "
                        f'"{target_name}", observed search query "{search_query}"'
                    ),
                    "expected_query": target_name,
                    "observed_query": search_query,
                    **last_artifact,
                }

            if state == "loading":
                await asyncio.sleep(0.6)
                continue

            if state == "account_profile" and target_visible:
                if account_type == "personal":
                    return {
                        "success": False,
                        "error": (
                            f'Opened "{target_name}", but the visible profile is '
                            "a personal account rather than a service account"
                        ),
                        **last_artifact,
                    }
                if (
                    expected_type == "service_account"
                    and account_type != "service_account"
                ):
                    return {
                        "success": False,
                        "error": (
                            f'Opened "{target_name}", but the profile did not '
                            "provide verified service-account evidence"
                        ),
                        "candidate_clicked": True,
                        "profile_opened": True,
                        "target_name": target_name,
                        "account_type": account_type,
                        "type_verified": False,
                        **last_artifact,
                    }
                if (
                    expected_type == "public_account"
                    and account_type
                    not in {"public_account", "service_account"}
                ):
                    return {
                        "success": False,
                        "error": (
                            f'Opened "{target_name}", but the profile did not '
                            "provide verified public-account evidence"
                        ),
                        "candidate_clicked": True,
                        "profile_opened": True,
                        "target_name": target_name,
                        "account_type": account_type,
                        "type_verified": False,
                        **last_artifact,
                    }
                return {
                    "success": True,
                    "summary": (
                        f'Opened verified WeChat account page "{target_name}"'
                    ),
                    "candidate_clicked": True,
                    "profile_opened": True,
                    "target_name": target_name,
                    "account_type": account_type,
                    "type_verified": account_type == "service_account",
                    "navigation_depth": navigation_actions,
                    "x": located["x"],
                    "y": located["y"],
                    "bbox": located.get("bbox"),
                    "confidence": located.get("confidence"),
                    "foreground_verified": True,
                    "vision_transport": config.vision.transport,
                    **last_artifact,
                }

            if state == "search_page":
                if navigation_actions >= 4:
                    break
                search_button = await locator.execute(
                    image_base64=screen["screenshot_base64"],
                    target=(
                        "微信搜一搜页面中用于提交当前搜索框关键词的可点击"
                        "搜索按钮；不要选择搜索输入框"
                    ),
                    image_width=int(screen["width"]),
                    image_height=int(screen["height"]),
                    screen_left=int(screen.get("left", 0)),
                    screen_top=int(screen.get("top", 0)),
                    min_confidence=min_confidence,
                )
                if not search_button.get("success"):
                    return {
                        "success": False,
                        "error": search_button.get(
                            "error",
                            "Cannot locate the WeChat in-page search button",
                        ),
                        **(_artifact_output(search_button) or last_artifact),
                    }
                search_click = await DesktopClickTool().execute(
                    x=int(search_button["x"]),
                    y=int(search_button["y"]),
                    confidence=float(search_button.get("confidence", 1.0)),
                    min_confidence=min_confidence,
                    app_name="wechat",
                    wait_after=0.8,
                )
                if not search_click.get("success"):
                    return {
                        "success": False,
                        "error": search_click.get(
                            "error",
                            "Could not submit the WeChat in-page search",
                        ),
                    }
                navigation_actions += 1
                continue

            if state != "search_results":
                return {
                    "success": False,
                    "error": (
                        f'Clicked "{target_name}", but did not reach its account '
                        f"page: {payload.get('reason', state)}"
                    ),
                    **last_artifact,
                }

            needs_official_tab = expected_type in {
                "service_account",
                "public_account",
            }
            account_tabs = {"accounts", "official_accounts"}
            if needs_official_tab and selected_tab not in account_tabs:
                if navigation_actions >= 4:
                    break
                normalized_tabs = {
                    str(tab).strip() for tab in available_tabs
                }
                tab_label = (
                    "账号"
                    if "账号" in normalized_tabs
                    else "公众号"
                )
                adjacent_label = (
                    "“小程序”等其他分类"
                    if tab_label == "账号"
                    else "相邻的“小程序”标签"
                )
                tab = await locator.execute(
                    image_base64=screen["screenshot_base64"],
                    target=(
                        f"微信搜一搜结果页顶部筛选栏中可点击的“{tab_label}”"
                        f"标签；不要选择{adjacent_label}"
                    ),
                    image_width=int(screen["width"]),
                    image_height=int(screen["height"]),
                    screen_left=int(screen.get("left", 0)),
                    screen_top=int(screen.get("top", 0)),
                    min_confidence=min_confidence,
                )
                if not tab.get("success"):
                    return {
                        "success": False,
                        "error": tab.get(
                            "error",
                            f"Cannot locate the WeChat {tab_label} filter tab",
                        ),
                        **(_artifact_output(tab) or last_artifact),
                    }
                tab_click = await DesktopClickTool().execute(
                    x=int(tab["x"]),
                    y=int(tab["y"]),
                    confidence=float(tab.get("confidence", 1.0)),
                    min_confidence=min_confidence,
                    app_name="wechat",
                    wait_after=0.8,
                )
                if not tab_click.get("success"):
                    return {
                        "success": False,
                        "error": tab_click.get(
                            "error",
                            f"Could not switch WeChat search to {tab_label}",
                        ),
                    }
                navigation_actions += 1
                continue

            if navigation_actions >= 4:
                break
            located = await locator.execute(
                image_base64=screen["screenshot_base64"],
                target=(
                    f'当前微信搜索结果中名称与“{target_name}”完全匹配、'
                    "可进入账号资料页的候选项；优先带服务号/公众号标识的结果，"
                    "不要选择仅部分匹配的名称。"
                ),
                image_width=int(screen["width"]),
                image_height=int(screen["height"]),
                screen_left=int(screen.get("left", 0)),
                screen_top=int(screen.get("top", 0)),
                min_confidence=min_confidence,
                expected_text=target_name,
                require_exact_text=True,
            )
            if not located.get("success"):
                return {
                    "success": False,
                    "error": located.get(
                        "error",
                        f'Cannot advance from search results to "{target_name}"',
                    ),
                    **(_artifact_output(located) or last_artifact),
                }
            clicked = await DesktopClickTool().execute(
                x=int(located["x"]),
                y=int(located["y"]),
                confidence=float(located.get("confidence", 1.0)),
                min_confidence=min_confidence,
                app_name="wechat",
                wait_after=0.8,
            )
            if not clicked.get("success"):
                return {
                    "success": False,
                    "error": clicked.get(
                        "error",
                        "Could not open WeChat account result",
                    ),
                }
            navigation_actions += 1

        return {
            "success": False,
            "error": (
                f'Could not reach the account page for "{target_name}" '
                f"after {navigation_actions} navigation actions"
            ),
            **last_artifact,
        }


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
                "expected_text": {
                    "type": "string",
                    "description": "需要与目标主文本逐字匹配的文本；为空则不检查",
                },
                "require_exact_text": {
                    "type": "boolean",
                    "description": "是否必须先验证 visible_text 与 expected_text 完全相等",
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
        expected_text: str = "",
        require_exact_text: bool = False,
    ) -> dict:
        if image_width <= 0 or image_height <= 0:
            return {"success": False, "error": "image_width and image_height must be positive"}
        threshold = max(0.0, min(float(min_confidence), 1.0))
        question = f"""在这张 {image_width}x{image_height} 像素的桌面截图中定位目标：{target}

只返回一个 JSON 对象，不要使用 Markdown，不要解释：
{{"found": true/false, "bbox": [left, top, right, bottom], "click_point": [x, y],
  "visible_text": "目标自身显示的主文本，非周围说明", "confidence": 0.0, "reason": "简短说明"}}

规则：
- 所有坐标都必须是相对于当前图片左上角的整数像素坐标；
- bbox 必须包围目标可点击区域，click_point 必须位于 bbox 内部；
- visible_text 必须逐字抄录目标自身的主标题；不得省略“下载”等后缀；
- 如果目标不可见、被遮挡、存在多个无法区分的候选，found=false；
- 不要猜测不可见目标。"""
        try:
            result, config = await _run_vision(
                image_base64, question, json_mode=True
            )
            payload = _parse_json_object(str(result.get("answer", "")))
            if payload.get("found") is not True:
                return {
                    "success": False,
                    "error": f"Target not found: {payload.get('reason', target)}",
                    **_artifact_output(result),
                }

            bbox = payload.get("bbox")
            point = payload.get("click_point")
            visible_text = str(payload.get("visible_text", "")).strip()
            confidence = float(payload.get("confidence", 0.0))
            if require_exact_text:
                wanted = _normalized_visible_text(expected_text)
                observed = _normalized_visible_text(visible_text)
                if not wanted:
                    raise ValueError(
                        "expected_text is required when require_exact_text=true"
                    )
                if observed != wanted:
                    return {
                        "success": False,
                        "error": (
                            "[TEXT_MISMATCH] Refusing to click: expected exact "
                            f'text "{expected_text}", observed "{visible_text or "missing"}"'
                        ),
                        "expected_text": expected_text,
                        "visible_text": visible_text,
                        **_artifact_output(result),
                    }
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValueError("Vision bbox must contain four coordinates")
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("Vision click_point must contain two coordinates")

            left, top, right, bottom = [int(round(float(value))) for value in bbox]
            rel_x, rel_y = [int(round(float(value))) for value in point]
            if not (0 <= left < right <= image_width and 0 <= top < bottom <= image_height):
                raise ValueError(f"Vision bbox is outside the {image_width}x{image_height} image")
            point_repaired = False
            if not (left <= rel_x <= right and top <= rel_y <= bottom):
                # The bounding box is still useful evidence. Clicking its
                # center is safer than discarding a correctly detected tab or
                # result because the model emitted an inconsistent point.
                rel_x = (left + right) // 2
                rel_y = (top + bottom) // 2
                point_repaired = True
                logger.warning(
                    "Vision click_point was outside bbox; using bbox center"
                )
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("Vision confidence must be between 0 and 1")
            if confidence < threshold:
                return {
                    "success": False,
                    "error": (
                        f"Target confidence {confidence:.2f} is below required "
                        f"{threshold:.2f}: {payload.get('reason', target)}"
                    ),
                    **_artifact_output(result),
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
                "click_point_repaired": point_repaired,
                "reason": str(payload.get("reason", "")),
                "visible_text": visible_text,
                "vision_provider": result.get("provider"),
                "vision_model": result.get("model"),
                "vision_transport": config.vision.transport,
                **_artifact_output(result),
            }
        except Exception as exc:
            err_msg = _error_message(exc)
            logger.error(f"Screen element location failed: {err_msg}")
            return {"success": False, "error": err_msg, **_artifact_output(exc)}
