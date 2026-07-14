from __future__ import annotations

import sys
from types import SimpleNamespace

from src.tools.desktop.text_input import _set_clipboard


def test_set_clipboard_uses_unicode_clipboard_format(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    fake_clipboard = SimpleNamespace(
        CF_UNICODETEXT=13,
        OpenClipboard=lambda: calls.append(("open", None)),
        EmptyClipboard=lambda: calls.append(("empty", None)),
        SetClipboardData=lambda fmt, value: calls.append(("set", (fmt, value))),
        CloseClipboard=lambda: calls.append(("close", None)),
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)

    _set_clipboard("大语言模型：技术原理")

    assert calls == [
        ("open", None),
        ("empty", None),
        ("set", (13, "大语言模型：技术原理")),
        ("close", None),
    ]


def test_set_clipboard_always_closes_on_failure(monkeypatch) -> None:
    calls: list[str] = []

    def fail_to_set(_fmt, _value) -> None:
        raise RuntimeError("clipboard unavailable")

    fake_clipboard = SimpleNamespace(
        CF_UNICODETEXT=13,
        OpenClipboard=lambda: calls.append("open"),
        EmptyClipboard=lambda: calls.append("empty"),
        SetClipboardData=fail_to_set,
        CloseClipboard=lambda: calls.append("close"),
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", fake_clipboard)

    try:
        _set_clipboard("中文")
    except RuntimeError as exc:
        assert str(exc) == "clipboard unavailable"
    else:
        raise AssertionError("expected clipboard failure")

    assert calls == ["open", "empty", "close"]
