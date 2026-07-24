from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.tools.ai import vision
from src.tools.ai.vision import (
    CheckWeChatLoginStatusTool,
    LocateScreenElementTool,
    OpenWeChatSearchCandidateTool,
)
from src.tools.base import ToolRegistry
from src.tools.registry_init import register_all_tools


@pytest.mark.asyncio
async def test_wechat_login_tool_returns_structured_logged_out_state(
    monkeypatch,
) -> None:
    async def fake_run(_image, _question, *, json_mode=False):
        assert json_mode is True
        return (
            {
                "answer": (
                    '{"logged_in": false, "state": "login_required", '
                    '"reason": "显示进入微信界面"}'
                ),
                "provider": "test",
                "model": "vision-test",
            },
            SimpleNamespace(
                vision=SimpleNamespace(transport="direct"),
            ),
        )

    monkeypatch.setattr(vision, "_run_vision", fake_run)

    result = await CheckWeChatLoginStatusTool().execute("image-data")

    assert result["success"] is True
    assert result["logged_in"] is False
    assert result["login_required"] is True
    assert result["state"] == "login_required"


@pytest.mark.asyncio
async def test_wechat_login_tool_is_conservative_for_non_boolean_true(
    monkeypatch,
) -> None:
    async def fake_run(_image, _question, *, json_mode=False):
        return (
            {
                "answer": (
                    '{"logged_in": "true", "state": "main_ui", '
                    '"reason": "ambiguous output"}'
                ),
            },
            SimpleNamespace(
                vision=SimpleNamespace(transport="direct"),
            ),
        )

    monkeypatch.setattr(vision, "_run_vision", fake_run)

    result = await CheckWeChatLoginStatusTool().execute("image-data")

    assert result["success"] is True
    assert result["logged_in"] is False
    assert result["state"] == "uncertain"


def test_wechat_login_status_tool_is_registered(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.utils.app_discovery.discover_apps",
        lambda: {},
    )
    registry = register_all_tools(ToolRegistry())

    assert registry.get("check_wechat_login_status") is not None
    assert registry.get("open_wechat_search_candidate") is not None


@pytest.mark.asyncio
async def test_open_wechat_candidate_locates_and_clicks_atomically(
    monkeypatch,
) -> None:
    async def fake_locate(self, **kwargs):
        assert "不要求" not in kwargs["target"]
        assert "不要要求" in kwargs["target"]
        return {
            "success": True,
            "x": 420,
            "y": 260,
            "bbox": [300, 220, 540, 300],
            "confidence": 0.94,
        }

    async def fake_click(self, **kwargs):
        assert kwargs["x"] == 420
        assert kwargs["y"] == 260
        assert kwargs["app_name"] == "wechat"
        return {
            "success": True,
            "foreground_verified": True,
        }

    async def fake_screenshot(self, app_name=""):
        assert app_name == "wechat"
        return {
            "success": True,
            "screenshot_base64": "after-click",
            "width": 1000,
            "height": 800,
            "left": 0,
            "top": 0,
        }

    async def fake_classify(_image, _question, *, json_mode=False):
        assert json_mode is True
        return (
            {
                "answer": (
                    '{"state": "account_profile", "target_visible": true, '
                    '"account_type": "service_account", '
                    '"reason": "已进入火眼审阅资料页"}'
                ),
            },
            SimpleNamespace(
                vision=SimpleNamespace(transport="direct"),
            ),
        )

    monkeypatch.setattr(
        vision.LocateScreenElementTool,
        "execute",
        fake_locate,
    )
    monkeypatch.setattr(
        "src.tools.desktop.native_control.DesktopClickTool.execute",
        fake_click,
    )
    monkeypatch.setattr(
        "src.tools.desktop.screen_capture.DesktopScreenshotTool.execute",
        fake_screenshot,
    )
    monkeypatch.setattr(vision, "_run_vision", fake_classify)

    result = await OpenWeChatSearchCandidateTool().execute(
        image_base64="image",
        target_name="火眼审阅",
        image_width=1000,
        image_height=800,
    )

    assert result["success"] is True
    assert result["candidate_clicked"] is True
    assert result["target_name"] == "火眼审阅"
    assert result["foreground_verified"] is True
    assert result["profile_opened"] is True
    assert result["type_verified"] is True
    assert result["account_type"] == "service_account"


@pytest.mark.asyncio
async def test_locator_repairs_click_point_with_valid_bbox_center(
    monkeypatch,
) -> None:
    async def fake_run(_image, _question, *, json_mode=False):
        return (
            {
                "answer": (
                    '{"found": true, "bbox": [100, 50, 300, 150], '
                    '"click_point": [900, 700], "confidence": 0.92, '
                    '"reason": "公众号标签"}'
                ),
            },
            SimpleNamespace(
                vision=SimpleNamespace(transport="direct"),
            ),
        )

    monkeypatch.setattr(vision, "_run_vision", fake_run)

    result = await LocateScreenElementTool().execute(
        image_base64="image",
        target="公众号标签",
        image_width=1000,
        image_height=800,
        screen_left=-1000,
        screen_top=20,
    )

    assert result["success"] is True
    assert result["relative_x"] == 200
    assert result["relative_y"] == 100
    assert result["x"] == -800
    assert result["y"] == 120
    assert result["click_point_repaired"] is True


@pytest.mark.asyncio
async def test_locator_rejects_related_but_not_exact_visible_text(
    monkeypatch,
) -> None:
    async def fake_run(_image, _question, *, json_mode=False):
        return (
            {
                "answer": (
                    '{"found": true, "bbox": [100, 50, 300, 150], '
                    '"click_point": [200, 100], "visible_text": "火眼审阅下载", '
                    '"confidence": 0.98, "reason": "相关搜索词"}'
                ),
            },
            SimpleNamespace(vision=SimpleNamespace(transport="direct")),
        )

    monkeypatch.setattr(vision, "_run_vision", fake_run)

    result = await LocateScreenElementTool().execute(
        image_base64="image",
        target="名称完全匹配的火眼审阅账号",
        image_width=1000,
        image_height=800,
        expected_text="火眼审阅",
        require_exact_text=True,
    )

    assert result["success"] is False
    assert result["error"].startswith("[TEXT_MISMATCH]")
    assert result["visible_text"] == "火眼审阅下载"


@pytest.mark.asyncio
async def test_open_wechat_service_candidate_switches_off_mini_programs(
    monkeypatch,
) -> None:
    locate_targets: list[str] = []
    clicks: list[tuple[int, int]] = []
    classifications = iter([
        (
            '{"state": "search_page", "target_visible": false, '
            '"account_type": "unknown", "selected_tab": "unknown", '
            '"available_tabs": [], "reason": "只有搜索框和搜索按钮"}'
        ),
        (
            '{"state": "search_results", "target_visible": false, '
            '"account_type": "unknown", "selected_tab": "mini_programs", '
            '"available_tabs": ["公众号", "小程序"], "reason": "当前是小程序"}'
        ),
        (
            '{"state": "search_results", "target_visible": true, '
            '"account_type": "service_account", '
            '"selected_tab": "official_accounts", '
            '"available_tabs": ["公众号", "小程序"], "reason": "公众号结果"}'
        ),
        (
            '{"state": "account_profile", "target_visible": true, '
            '"account_type": "service_account", '
            '"selected_tab": "unknown", "available_tabs": [], '
            '"reason": "已进入火眼审阅主页"}'
        ),
    ])

    async def fake_locate(self, **kwargs):
        locate_targets.append(kwargs["target"])
        index = len(locate_targets)
        return {
            "success": True,
            "x": 100 + index,
            "y": 200 + index,
            "bbox": [50, 50, 150, 100],
            "confidence": 0.95,
        }

    async def fake_click(self, **kwargs):
        clicks.append((kwargs["x"], kwargs["y"]))
        return {"success": True, "foreground_verified": True}

    async def fake_screenshot(self, app_name=""):
        return {
            "success": True,
            "screenshot_base64": "screen",
            "width": 1000,
            "height": 800,
            "left": 0,
            "top": 0,
        }

    async def fake_classify(_image, _question, *, json_mode=False):
        return (
            {"answer": next(classifications)},
            SimpleNamespace(
                vision=SimpleNamespace(transport="direct"),
            ),
        )

    monkeypatch.setattr(
        vision.LocateScreenElementTool,
        "execute",
        fake_locate,
    )
    monkeypatch.setattr(
        "src.tools.desktop.native_control.DesktopClickTool.execute",
        fake_click,
    )
    monkeypatch.setattr(
        "src.tools.desktop.screen_capture.DesktopScreenshotTool.execute",
        fake_screenshot,
    )
    monkeypatch.setattr(vision, "_run_vision", fake_classify)

    result = await OpenWeChatSearchCandidateTool().execute(
        image_base64="initial",
        target_name="火眼审阅",
        expected_type="service_account",
        image_width=1000,
        image_height=800,
    )

    assert result["success"] is True
    assert result["profile_opened"] is True
    assert result["type_verified"] is True
    assert len(clicks) == 4
    assert any("搜索按钮" in target for target in locate_targets)
    assert any("公众号" in target for target in locate_targets)
    assert any("不要选择相邻的“小程序”" in target for target in locate_targets)


@pytest.mark.asyncio
async def test_open_wechat_service_candidate_uses_new_accounts_tab(
    monkeypatch,
) -> None:
    locate_targets: list[str] = []
    exact_requests: list[str] = []
    clicks: list[tuple[int, int]] = []
    classifications = iter([
        (
            '{"state": "search_results", "target_visible": true, '
            '"account_type": "unknown", "selected_tab": "other", '
            '"available_tabs": ["AI搜索", "全部", "账号", "文章", "小程序"], '
            '"reason": "当前在全部结果页"}'
        ),
        (
            '{"state": "search_results", "target_visible": true, '
            '"account_type": "unknown", "selected_tab": "accounts", '
            '"available_tabs": ["AI搜索", "全部", "账号", "文章", "小程序"], '
            '"reason": "账号分类已选中且火眼审阅可见"}'
        ),
        (
            '{"state": "account_profile", "target_visible": true, '
            '"account_type": "service_account", "selected_tab": "unknown", '
            '"available_tabs": [], "reason": "资料页显示服务号"}'
        ),
    ])

    async def fake_locate(self, **kwargs):
        locate_targets.append(kwargs["target"])
        if kwargs.get("require_exact_text"):
            exact_requests.append(kwargs.get("expected_text", ""))
        index = len(locate_targets)
        return {
            "success": True,
            "x": 300 + index,
            "y": 400 + index,
            "bbox": [250, 350, 450, 430],
            "confidence": 0.96,
        }

    async def fake_click(self, **kwargs):
        clicks.append((kwargs["x"], kwargs["y"]))
        return {"success": True, "foreground_verified": True}

    async def fake_screenshot(self, app_name=""):
        return {
            "success": True,
            "screenshot_base64": "screen",
            "width": 1000,
            "height": 800,
            "left": 0,
            "top": 0,
        }

    async def fake_classify(_image, _question, *, json_mode=False):
        return (
            {"answer": next(classifications)},
            SimpleNamespace(vision=SimpleNamespace(transport="direct")),
        )

    monkeypatch.setattr(
        vision.LocateScreenElementTool,
        "execute",
        fake_locate,
    )
    monkeypatch.setattr(
        "src.tools.desktop.native_control.DesktopClickTool.execute",
        fake_click,
    )
    monkeypatch.setattr(
        "src.tools.desktop.screen_capture.DesktopScreenshotTool.execute",
        fake_screenshot,
    )
    monkeypatch.setattr(vision, "_run_vision", fake_classify)

    result = await OpenWeChatSearchCandidateTool().execute(
        image_base64="initial",
        target_name="火眼审阅",
        expected_type="service_account",
        image_width=1000,
        image_height=800,
    )

    assert result["success"] is True
    assert result["type_verified"] is True
    assert len(clicks) == 3
    assert any("“账号”" in target for target in locate_targets)
    assert any(
        "名称与“火眼审阅”完全匹配" in target
        for target in locate_targets
    )
    assert exact_requests == ["火眼审阅", "火眼审阅"]


@pytest.mark.asyncio
async def test_open_wechat_service_candidate_rejects_unknown_profile_type(
    monkeypatch,
) -> None:
    async def fake_locate(self, **kwargs):
        return {
            "success": True,
            "x": 100,
            "y": 200,
            "bbox": [50, 100, 150, 250],
            "confidence": 0.95,
        }

    async def fake_click(self, **kwargs):
        return {"success": True, "foreground_verified": True}

    async def fake_screenshot(self, app_name=""):
        return {
            "success": True,
            "screenshot_base64": "screen",
            "width": 1000,
            "height": 800,
            "left": 0,
            "top": 0,
        }

    async def fake_classify(_image, _question, *, json_mode=False):
        return (
            {
                "answer": (
                    '{"state": "account_profile", "target_visible": true, '
                    '"account_type": "unknown", "selected_tab": "unknown", '
                    '"available_tabs": [], "reason": "名称可见但类型不可见"}'
                ),
            },
            SimpleNamespace(vision=SimpleNamespace(transport="direct")),
        )

    monkeypatch.setattr(
        vision.LocateScreenElementTool,
        "execute",
        fake_locate,
    )
    monkeypatch.setattr(
        "src.tools.desktop.native_control.DesktopClickTool.execute",
        fake_click,
    )
    monkeypatch.setattr(
        "src.tools.desktop.screen_capture.DesktopScreenshotTool.execute",
        fake_screenshot,
    )
    monkeypatch.setattr(vision, "_run_vision", fake_classify)

    result = await OpenWeChatSearchCandidateTool().execute(
        image_base64="initial",
        target_name="火眼审阅",
        expected_type="service_account",
        image_width=1000,
        image_height=800,
    )

    assert result["success"] is False
    assert result["profile_opened"] is True
    assert result["type_verified"] is False
    assert "verified service-account evidence" in result["error"]


@pytest.mark.asyncio
async def test_open_wechat_candidate_waits_through_transient_loading(
    monkeypatch,
) -> None:
    classifications = iter([
        (
            '{"state": "loading", "target_visible": false, '
            '"account_type": "unknown", "selected_tab": "unknown", '
            '"available_tabs": [], "reason": "结果区正在加载"}'
        ),
        (
            '{"state": "account_profile", "target_visible": true, '
            '"account_type": "service_account", "selected_tab": "unknown", '
            '"available_tabs": [], "reason": "服务号资料页已加载"}'
        ),
    ])
    screenshots = 0
    clicks = 0

    async def fake_locate(self, **kwargs):
        return {
            "success": True,
            "x": 100,
            "y": 200,
            "bbox": [50, 100, 150, 250],
            "confidence": 0.95,
        }

    async def fake_click(self, **kwargs):
        nonlocal clicks
        clicks += 1
        return {"success": True, "foreground_verified": True}

    async def fake_screenshot(self, app_name=""):
        nonlocal screenshots
        screenshots += 1
        return {
            "success": True,
            "screenshot_base64": "screen",
            "width": 1000,
            "height": 800,
            "left": 0,
            "top": 0,
        }

    async def fake_classify(_image, _question, *, json_mode=False):
        return (
            {"answer": next(classifications)},
            SimpleNamespace(vision=SimpleNamespace(transport="direct")),
        )

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(
        vision.LocateScreenElementTool,
        "execute",
        fake_locate,
    )
    monkeypatch.setattr(
        "src.tools.desktop.native_control.DesktopClickTool.execute",
        fake_click,
    )
    monkeypatch.setattr(
        "src.tools.desktop.screen_capture.DesktopScreenshotTool.execute",
        fake_screenshot,
    )
    monkeypatch.setattr(vision, "_run_vision", fake_classify)
    monkeypatch.setattr(vision.asyncio, "sleep", no_wait)

    result = await OpenWeChatSearchCandidateTool().execute(
        image_base64="initial",
        target_name="火眼审阅",
        expected_type="service_account",
        image_width=1000,
        image_height=800,
    )

    assert result["success"] is True
    assert screenshots == 2
    assert clicks == 1
    assert result["navigation_depth"] == 1


@pytest.mark.asyncio
async def test_open_wechat_candidate_stops_when_search_query_drifted(
    monkeypatch,
) -> None:
    clicks = 0

    async def fake_locate(self, **kwargs):
        return {
            "success": True,
            "x": 100,
            "y": 200,
            "bbox": [50, 100, 150, 250],
            "confidence": 0.95,
            "visible_text": kwargs.get("expected_text", ""),
        }

    async def fake_click(self, **kwargs):
        nonlocal clicks
        clicks += 1
        return {"success": True, "foreground_verified": True}

    async def fake_screenshot(self, app_name=""):
        return {
            "success": True,
            "screenshot_base64": "screen",
            "width": 1000,
            "height": 800,
            "left": 0,
            "top": 0,
        }

    async def fake_classify(_image, _question, *, json_mode=False):
        return (
            {
                "answer": (
                    '{"state": "search_results", "target_visible": true, '
                    '"account_type": "unknown", "selected_tab": "accounts", '
                    '"available_tabs": ["账号"], '
                    '"search_query": "火眼审阅下载", '
                    '"reason": "搜索词已被相关建议替换"}'
                ),
            },
            SimpleNamespace(vision=SimpleNamespace(transport="direct")),
        )

    monkeypatch.setattr(
        vision.LocateScreenElementTool,
        "execute",
        fake_locate,
    )
    monkeypatch.setattr(
        "src.tools.desktop.native_control.DesktopClickTool.execute",
        fake_click,
    )
    monkeypatch.setattr(
        "src.tools.desktop.screen_capture.DesktopScreenshotTool.execute",
        fake_screenshot,
    )
    monkeypatch.setattr(vision, "_run_vision", fake_classify)

    result = await OpenWeChatSearchCandidateTool().execute(
        image_base64="initial",
        target_name="火眼审阅",
        expected_type="service_account",
        image_width=1000,
        image_height=800,
    )

    assert result["success"] is False
    assert result["error"].startswith("[SEARCH_QUERY_DRIFT]")
    assert result["observed_query"] == "火眼审阅下载"
    assert clicks == 1
