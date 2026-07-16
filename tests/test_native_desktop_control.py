from __future__ import annotations

from src.tools.desktop import native_control


def test_window_aliases_include_chinese_wechat_title() -> None:
    assert native_control._window_tokens("wechat") == ("微信", "wechat")


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
