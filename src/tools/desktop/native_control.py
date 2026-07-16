"""Native Windows window focus, keyboard and pointer controls."""

from __future__ import annotations

import ctypes
import time
from typing import Any

from src.tools.base import BaseTool, ToolSchema
from src.utils.windows_dpi import enable_per_monitor_dpi_awareness


_WINDOW_ALIASES = {
    "wechat": ("微信", "wechat"),
    "weixin": ("微信", "wechat"),
    "微信": ("微信", "wechat"),
    "word": ("word", "文档"),
    "wps": ("wps", "文字"),
}

_VK_CODES = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "shift": 0x10,
    "ctrl": 0x11,
    "alt": 0x12,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
}
_MODIFIERS = {"ctrl", "alt", "shift"}

_MOUSE_FLAGS = {
    "left": (0x0002, 0x0004),
    "right": (0x0008, 0x0010),
}


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _user32():
    enable_per_monitor_dpi_awareness()
    return ctypes.windll.user32


def _virtual_screen_bounds() -> tuple[int, int, int, int]:
    user32 = _user32()
    left = int(user32.GetSystemMetrics(76))   # SM_XVIRTUALSCREEN
    top = int(user32.GetSystemMetrics(77))    # SM_YVIRTUALSCREEN
    width = int(user32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
    height = int(user32.GetSystemMetrics(79)) # SM_CYVIRTUALSCREEN
    return left, top, left + width, top + height


def _validate_screen_point(x: int, y: int) -> None:
    left, top, right, bottom = _virtual_screen_bounds()
    if right <= left or bottom <= top:
        raise RuntimeError("Cannot determine virtual screen bounds")
    if not (left <= x < right and top <= y < bottom):
        raise ValueError(
            f"Point ({x}, {y}) is outside virtual screen "
            f"({left}, {top})-({right}, {bottom})"
        )


def _move_cursor(x: int, y: int, duration: float = 0.0) -> None:
    x, y = int(x), int(y)
    _validate_screen_point(x, y)
    user32 = _user32()
    duration = max(0.0, min(float(duration), 5.0))
    if duration <= 0:
        if not user32.SetCursorPos(x, y):
            raise RuntimeError(f"Cannot move pointer to ({x}, {y})")
        return

    start = _Point()
    if not user32.GetCursorPos(ctypes.byref(start)):
        if not user32.SetCursorPos(x, y):
            raise RuntimeError(f"Cannot move pointer to ({x}, {y})")
        return
    steps = max(2, min(int(duration * 60), 120))
    for index in range(1, steps + 1):
        ratio = index / steps
        current_x = round(start.x + (x - start.x) * ratio)
        current_y = round(start.y + (y - start.y) * ratio)
        user32.SetCursorPos(current_x, current_y)
        time.sleep(duration / steps)


def _mouse_button_event(button: str, pressed: bool) -> None:
    if button not in _MOUSE_FLAGS:
        raise ValueError(f"Unsupported mouse button: {button}")
    down, up = _MOUSE_FLAGS[button]
    _user32().mouse_event(down if pressed else up, 0, 0, 0, 0)


def _window_tokens(app_name: str) -> tuple[str, ...]:
    normalized = app_name.strip().lower()
    return _WINDOW_ALIASES.get(normalized, (normalized,))


def _find_window(app_name: str) -> tuple[int, str] | None:
    import win32gui

    tokens = _window_tokens(app_name)
    matches: list[tuple[int, str]] = []

    def collect(hwnd: int, _extra: Any) -> None:
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd).strip()
        lowered = title.lower()
        if title and any(token in lowered for token in tokens):
            matches.append((hwnd, title))

    win32gui.EnumWindows(collect, None)
    if not matches:
        return None
    return min(matches, key=lambda item: len(item[1]))


def _root_window(hwnd: int) -> int:
    import win32gui

    root = int(win32gui.GetAncestor(hwnd, 2))  # GA_ROOT
    return root or int(hwnd)


def _foreground_matches(hwnd: int) -> bool:
    import win32gui

    foreground = int(win32gui.GetForegroundWindow())
    if not foreground:
        return False
    return _root_window(foreground) == _root_window(hwnd)


def _focus_window(hwnd: int) -> None:
    import win32con
    import win32gui

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        # Windows may reject foreground activation until the caller generates
        # input. A harmless Alt press grants the foreground transition.
        user32 = _user32()
        user32.keybd_event(_VK_CODES["alt"], 0, 0, 0)
        user32.keybd_event(_VK_CODES["alt"], 0, 0x0002, 0)
        win32gui.SetForegroundWindow(hwnd)


def activate_window(app_name: str, timeout: float = 2.0) -> tuple[int, str]:
    """Focus a named app and verify it really owns the foreground before input."""
    match = _find_window(app_name)
    if match is None:
        raise RuntimeError(f"No visible window matched '{app_name}'")
    hwnd, title = match
    deadline = time.monotonic() + max(0.2, min(float(timeout), 5.0))
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _focus_window(hwnd)
        except Exception as exc:
            last_error = exc
        time.sleep(0.08)
        if _foreground_matches(hwnd):
            return hwnd, title
    detail = f": {last_error}" if last_error else ""
    raise RuntimeError(
        f"Target window '{title}' did not become foreground; input was not sent{detail}"
    )
def _key_code(name: str) -> int:
    lowered = name.strip().lower()
    if lowered in _VK_CODES:
        return _VK_CODES[lowered]
    if len(lowered) == 1 and lowered.isascii() and lowered.isalnum():
        return ord(lowered.upper())
    raise ValueError(f"Unsupported key: {name}")


def _send_key_chord(keys: str) -> None:
    parts = [part.strip().lower() for part in keys.split("+") if part.strip()]
    if not parts or len(parts) > 4:
        raise ValueError("keys must be a key or chord such as enter / ctrl+f / shift+tab")
    if any(part in _MODIFIERS for part in parts[-1:]):
        raise ValueError("A key chord must end with a non-modifier key")

    codes = [_key_code(part) for part in parts]
    user32 = _user32()
    for code in codes:
        user32.keybd_event(code, 0, 0, 0)
    for code in reversed(codes):
        user32.keybd_event(code, 0, 0x0002, 0)


class FocusWindowTool(BaseTool):
    schema = ToolSchema(
        name="focus_window",
        description=(
            "将原生桌面应用窗口恢复并切换到前台。操作微信等桌面客户端前必须先调用。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "应用或窗口名称，如 wechat / 微信 / word",
                },
                "wait_after": {
                    "type": "number",
                    "description": "聚焦后等待秒数，默认 0.5",
                },
            },
            "required": ["app_name"],
        },
    )

    async def execute(self, app_name: str, wait_after: float = 0.5) -> dict:
        try:
            hwnd, title = activate_window(app_name)
            time.sleep(max(0.0, min(wait_after, 5.0)))
            return {
                "success": True,
                "summary": f"Focused window: {title}",
                "window_title": title,
                "window_handle": hwnd,
            }
        except ImportError as exc:
            return {"success": False, "error": f"[ENV_ERR] Missing dependency: {exc}"}
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class DesktopKeypressTool(BaseTool):
    schema = ToolSchema(
        name="desktop_keypress",
        description=(
            "向当前聚焦的原生桌面应用发送按键或快捷键。"
            "每次发送前会按 app_name 重新聚焦并验证目标窗口。"
            "支持 enter、tab、escape、方向键、ctrl+f、ctrl+a、shift+tab 等。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "keys": {
                    "type": "string",
                    "description": "按键或组合键，如 enter、ctrl+f、ctrl+a、shift+tab",
                },
                "app_name": {
                    "type": "string",
                    "description": "必须接收按键的目标应用，如 wechat / 微信 / word",
                },
                "presses": {
                    "type": "integer",
                    "description": "重复次数，默认 1，最大 10",
                },
                "wait_after": {
                    "type": "number",
                    "description": "按键后等待秒数，默认 0.3",
                },
            },
            "required": ["keys", "app_name"],
        },
    )

    async def execute(
        self, keys: str, app_name: str, presses: int = 1, wait_after: float = 0.3
    ) -> dict:
        try:
            hwnd, title = activate_window(app_name)
            count = max(1, min(int(presses), 10))
            for _ in range(count):
                _send_key_chord(keys)
                time.sleep(0.05)
            time.sleep(max(0.0, min(wait_after, 5.0)))
            return {
                "success": True,
                "summary": f"Pressed {keys} x{count} in verified window: {title}",
                "keys": keys,
                "presses": count,
                "target_app": app_name,
                "window_title": title,
                "window_handle": hwnd,
                "foreground_verified": True,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class DesktopClickTool(BaseTool):
    schema = ToolSchema(
        name="desktop_click",
        description=(
            "点击原生桌面屏幕坐标。仅在截图分析明确给出目标中心坐标后使用；"
            "浏览器页面应继续使用 click 工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "屏幕横坐标"},
                "y": {"type": "integer", "description": "屏幕纵坐标"},
                "button": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "description": "鼠标按键，默认 left",
                },
                "clicks": {
                    "type": "integer",
                    "description": "点击次数，1=单击，2=双击",
                },
                "confidence": {
                    "type": "number",
                    "description": "视觉定位置信度；来自 locate_screen_element.confidence",
                },
                "min_confidence": {
                    "type": "number",
                    "description": "最低置信度，默认 0.70",
                },
                "wait_after": {
                    "type": "number",
                    "description": "点击后等待秒数，默认 0.3",
                },
            },
            "required": ["x", "y"],
        },
    )

    async def execute(
        self,
        x: int,
        y: int,
        button: str = "left",
        clicks: int = 1,
        confidence: float = 1.0,
        min_confidence: float = 0.70,
        wait_after: float = 0.3,
    ) -> dict:
        try:
            confidence = float(confidence)
            threshold = max(0.0, min(float(min_confidence), 1.0))
            if not 0.0 <= confidence <= 1.0:
                return {"success": False, "error": "confidence must be between 0 and 1"}
            if confidence < threshold:
                return {
                    "success": False,
                    "error": f"Click confidence {confidence:.2f} is below {threshold:.2f}",
                }
            count = max(1, min(int(clicks), 2))
            _move_cursor(int(x), int(y))
            for _ in range(count):
                _mouse_button_event(button, True)
                _mouse_button_event(button, False)
                time.sleep(0.08)
            time.sleep(max(0.0, min(wait_after, 5.0)))
            return {
                "success": True,
                "summary": f"{button.title()} clicked ({x}, {y}) x{count}",
                "x": x,
                "y": y,
                "button": button,
                "clicks": count,
                "confidence": confidence,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class DesktopMoveMouseTool(BaseTool):
    schema = ToolSchema(
        name="desktop_move_mouse",
        description="将鼠标移动到物理屏幕坐标，不执行点击。支持多显示器负坐标。",
        parameters={
            "type": "object",
            "properties": {
                "x": {"type": "integer", "description": "目标屏幕 X"},
                "y": {"type": "integer", "description": "目标屏幕 Y"},
                "duration": {"type": "number", "description": "移动持续秒数，默认 0.2"},
            },
            "required": ["x", "y"],
        },
    )

    async def execute(self, x: int, y: int, duration: float = 0.2) -> dict:
        try:
            _move_cursor(x, y, duration)
            return {
                "success": True,
                "summary": f"Moved pointer to ({x}, {y})",
                "x": x,
                "y": y,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class DesktopScrollTool(BaseTool):
    schema = ToolSchema(
        name="desktop_scroll",
        description="在当前鼠标位置滚动原生桌面窗口；正数向上，负数向下。",
        parameters={
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "description": "滚动格数，范围 -20 到 20，负数向下",
                },
                "wait_after": {"type": "number", "description": "滚动后等待秒数"},
            },
            "required": ["amount"],
        },
    )

    async def execute(self, amount: int, wait_after: float = 0.3) -> dict:
        try:
            amount = max(-20, min(int(amount), 20))
            if amount == 0:
                return {"success": False, "error": "amount cannot be zero"}
            _user32().mouse_event(0x0800, 0, 0, amount * 120, 0)
            time.sleep(max(0.0, min(wait_after, 5.0)))
            return {
                "success": True,
                "summary": f"Scrolled {amount} wheel steps",
                "amount": amount,
            }
        except Exception as exc:
            return {"success": False, "error": str(exc)}


class DesktopDragTool(BaseTool):
    schema = ToolSchema(
        name="desktop_drag",
        description="从一个物理屏幕坐标按住左键拖拽到另一个坐标。",
        parameters={
            "type": "object",
            "properties": {
                "start_x": {"type": "integer", "description": "起点 X"},
                "start_y": {"type": "integer", "description": "起点 Y"},
                "end_x": {"type": "integer", "description": "终点 X"},
                "end_y": {"type": "integer", "description": "终点 Y"},
                "duration": {"type": "number", "description": "拖拽秒数，默认 0.5"},
                "wait_after": {"type": "number", "description": "拖拽后等待秒数"},
            },
            "required": ["start_x", "start_y", "end_x", "end_y"],
        },
    )

    async def execute(
        self,
        start_x: int,
        start_y: int,
        end_x: int,
        end_y: int,
        duration: float = 0.5,
        wait_after: float = 0.3,
    ) -> dict:
        pressed = False
        try:
            _move_cursor(start_x, start_y)
            _mouse_button_event("left", True)
            pressed = True
            _move_cursor(end_x, end_y, duration)
            _mouse_button_event("left", False)
            pressed = False
            time.sleep(max(0.0, min(wait_after, 5.0)))
            return {
                "success": True,
                "summary": f"Dragged ({start_x}, {start_y}) to ({end_x}, {end_y})",
                "start": [start_x, start_y],
                "end": [end_x, end_y],
            }
        except Exception as exc:
            if pressed:
                try:
                    _mouse_button_event("left", False)
                except Exception:
                    pass
            return {"success": False, "error": str(exc)}
