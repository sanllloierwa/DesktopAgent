from __future__ import annotations

import sys
from types import SimpleNamespace

from src.tools.desktop import native_control


def test_window_aliases_include_chinese_wechat_title() -> None:
    assert native_control._window_tokens("wechat") == (
        "微信",
        "wechat",
        "weixin",
    )


def test_wechat_window_selection_prefers_main_unowned_window(
    monkeypatch,
) -> None:
    windows = {
        1: {"title": "微信", "rect": (0, 0, 1200, 800), "owner": 0},
        2: {"title": "微信", "rect": (300, 200, 700, 500), "owner": 1},
        3: {"title": "微信登录", "rect": (0, 0, 500, 600), "owner": 0},
    }

    def enum_windows(callback, extra):
        for hwnd in windows:
            callback(hwnd, extra)

    fake_win32gui = SimpleNamespace(
        EnumWindows=enum_windows,
        IsWindowVisible=lambda _hwnd: True,
        GetWindowText=lambda hwnd: windows[hwnd]["title"],
        GetWindowRect=lambda hwnd: windows[hwnd]["rect"],
        GetWindow=lambda hwnd, _flag: windows[hwnd]["owner"],
    )
    monkeypatch.setitem(sys.modules, "win32gui", fake_win32gui)
    monkeypatch.setattr(
        native_control,
        "_window_process_name",
        lambda _hwnd: "weixin.exe",
    )

    assert native_control._find_window("wechat") == (1, "微信")


def test_wechat_window_selection_rejects_explorer_title_collision(
    monkeypatch,
) -> None:
    windows = {
        10: {
            "title": "WeChat Files - 文件资源管理器",
            "rect": (0, 0, 1600, 1000),
            "owner": 0,
            "process": "explorer.exe",
        },
        20: {
            "title": "微信",
            "rect": (100, 100, 1100, 800),
            "owner": 0,
            "process": "weixin.exe",
        },
    }

    def enum_windows(callback, extra):
        for hwnd in windows:
            callback(hwnd, extra)

    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            EnumWindows=enum_windows,
            IsWindowVisible=lambda _hwnd: True,
            GetWindowText=lambda hwnd: windows[hwnd]["title"],
            GetWindowRect=lambda hwnd: windows[hwnd]["rect"],
            GetWindow=lambda hwnd, _flag: windows[hwnd]["owner"],
        ),
    )
    monkeypatch.setattr(
        native_control,
        "_window_process_name",
        lambda hwnd: windows[hwnd]["process"],
    )

    assert native_control._find_window("wechat") == (20, "微信")


def test_wechat_window_selection_rejects_unknown_process(
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            EnumWindows=lambda callback, extra: callback(10, extra),
            IsWindowVisible=lambda _hwnd: True,
            GetWindowText=lambda _hwnd: "微信备份文件",
            GetWindowRect=lambda _hwnd: (0, 0, 1200, 800),
            GetWindow=lambda _hwnd, _flag: 0,
        ),
    )
    monkeypatch.setattr(
        native_control,
        "_window_process_name",
        lambda _hwnd: "",
    )

    assert native_control._find_window("wechat") is None


def test_wechat_window_selection_accepts_process_with_nonstandard_title(
    monkeypatch,
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(
            EnumWindows=lambda callback, extra: callback(25, extra),
            IsWindowVisible=lambda _hwnd: True,
            GetWindowText=lambda _hwnd: "即时通讯",
            GetWindowRect=lambda _hwnd: (100, 100, 1100, 800),
            GetWindow=lambda _hwnd, _flag: 0,
        ),
    )
    monkeypatch.setattr(
        native_control,
        "_window_process_name",
        lambda _hwnd: "weixin.exe",
    )

    assert native_control._find_window("wechat") == (25, "即时通讯")


def test_key_codes_support_wechat_search_shortcut() -> None:
    assert native_control._key_code("ctrl") == 0x11
    assert native_control._key_code("f") == ord("F")
    assert native_control._key_code("enter") == 0x0D


async def test_desktop_keypress_repeats_and_reports_result(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(native_control, "_send_key_chord", calls.append)
    monkeypatch.setattr(native_control, "activate_window", lambda _app: (123, "微信"))
    monkeypatch.setattr(native_control.time, "sleep", lambda _seconds: None)

    result = await native_control.DesktopKeypressTool().execute(
        "ctrl+f", app_name="wechat", presses=2, wait_after=0
    )

    assert result["success"] is True
    assert calls == ["ctrl+f", "ctrl+f"]
    assert result["foreground_verified"] is True
    assert result["target_app"] == "wechat"


async def test_focus_window_returns_clear_failure(monkeypatch) -> None:
    monkeypatch.setattr(native_control, "_find_window", lambda _name: None)

    result = await native_control.FocusWindowTool().execute("wechat", wait_after=0)

    assert result["success"] is False
    assert "No visible window" in result["error"]


async def test_keypress_sends_nothing_when_target_cannot_be_verified(monkeypatch) -> None:
    calls: list[str] = []

    def fail_activation(_app):
        raise RuntimeError("Target window did not become foreground")

    monkeypatch.setattr(native_control, "activate_window", fail_activation)
    monkeypatch.setattr(native_control, "_send_key_chord", calls.append)

    result = await native_control.DesktopKeypressTool().execute(
        "enter", app_name="wechat"
    )

    assert result["success"] is False
    assert calls == []
    assert "did not become foreground" in result["error"]


def test_activate_window_requires_verified_foreground(monkeypatch) -> None:
    clock = iter([0.0, 0.0, 0.3])
    monkeypatch.setattr(native_control, "_find_window", lambda _app: (123, "微信"))
    monkeypatch.setattr(native_control, "_focus_window", lambda _hwnd: None)
    monkeypatch.setattr(native_control, "_foreground_matches", lambda _hwnd: False)
    monkeypatch.setattr(native_control.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(native_control.time, "sleep", lambda _seconds: None)

    try:
        native_control.activate_window("wechat", timeout=0.2)
    except RuntimeError as exc:
        assert "input was not sent" in str(exc)
    else:
        raise AssertionError("expected foreground verification failure")


async def test_click_accepts_negative_multi_monitor_coordinate(monkeypatch) -> None:
    moves: list[tuple[int, int, float]] = []
    buttons: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        native_control,
        "_move_cursor",
        lambda x, y, duration=0: moves.append((x, y, duration)),
    )
    monkeypatch.setattr(
        native_control,
        "_mouse_button_event",
        lambda button, pressed: buttons.append((button, pressed)),
    )
    monkeypatch.setattr(native_control.time, "sleep", lambda _seconds: None)

    result = await native_control.DesktopClickTool().execute(
        -1680, 330, button="right", clicks=2, confidence=0.93, wait_after=0
    )

    assert result["success"] is True
    assert moves == [(-1680, 330, 0)]
    assert buttons == [
        ("right", True), ("right", False),
        ("right", True), ("right", False),
    ]


async def test_click_rejects_low_visual_confidence(monkeypatch) -> None:
    monkeypatch.setattr(
        native_control,
        "_move_cursor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not move")),
    )

    result = await native_control.DesktopClickTool().execute(
        100, 100, confidence=0.4, min_confidence=0.7
    )

    assert result["success"] is False
    assert "below" in result["error"]


async def test_click_refocuses_and_verifies_target_app(monkeypatch) -> None:
    actions: list[tuple] = []
    monkeypatch.setattr(
        native_control,
        "activate_window",
        lambda app: actions.append(("focus", app)) or (321, "微信"),
    )
    monkeypatch.setattr(
        native_control,
        "_move_cursor",
        lambda x, y, duration=0: actions.append(("move", x, y)),
    )
    monkeypatch.setattr(
        native_control,
        "_mouse_button_event",
        lambda button, pressed: actions.append(("button", button, pressed)),
    )
    monkeypatch.setattr(native_control.time, "sleep", lambda _seconds: None)

    result = await native_control.DesktopClickTool().execute(
        100, 200, app_name="wechat", confidence=0.9, wait_after=0
    )

    assert result["success"] is True
    assert actions[0] == ("focus", "wechat")
    assert actions[1] == ("move", 100, 200)
    assert result["foreground_verified"] is True
    assert result["window_handle"] == 321
    assert result["target_app"] == "wechat"


async def test_drag_releases_mouse_if_move_fails(monkeypatch) -> None:
    moves = 0
    buttons: list[tuple[str, bool]] = []

    def fake_move(*_args, **_kwargs):
        nonlocal moves
        moves += 1
        if moves == 2:
            raise RuntimeError("move failed")

    monkeypatch.setattr(native_control, "_move_cursor", fake_move)
    monkeypatch.setattr(
        native_control,
        "_mouse_button_event",
        lambda button, pressed: buttons.append((button, pressed)),
    )

    result = await native_control.DesktopDragTool().execute(10, 10, 20, 20)

    assert result["success"] is False
    assert buttons == [("left", True), ("left", False)]
