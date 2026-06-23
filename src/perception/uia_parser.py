"""UIA tree parser — Windows UI Automation 控件树解析"""

from __future__ import annotations

from typing import Any

from loguru import logger


async def parse_uia_tree(max_depth: int = 4, filter_interactive: bool = True) -> dict[str, Any]:
    """解析当前前景窗口的 UIA 树，返回可读摘要。

    Returns:
        {"success": True, "summary": "Button 'OK' | Edit '姓名' | ..."}
    """
    try:
        import uiautomation as auto

        window = auto.GetForegroundControl()
        if window is None:
            return {"success": False, "error": "No foreground window"}

        lines: list[str] = []
        _walk_uia(window, lines, depth=0, max_depth=max_depth, filter_interactive=filter_interactive)

        return {
            "success": True,
            "summary": "\n".join(lines),
            "window_name": window.Name,
            "control_count": len(lines),
        }
    except ImportError:
        return {"success": False, "error": "uiautomation not installed"}
    except Exception as exc:
        logger.error(f"UIA parse failed: {exc}")
        return {"success": False, "error": str(exc)}


_INTERACTIVE_TYPES = {
    "ButtonControl", "EditControl", "ComboBoxControl", "CheckBoxControl",
    "RadioButtonControl", "ListControl", "ListItemControl", "HyperlinkControl",
    "TabItemControl", "TreeItemControl", "MenuItemControl", "TextBoxControl",
}


def _walk_uia(control, lines: list[str], depth: int, max_depth: int, filter_interactive: bool) -> None:
    if depth > max_depth:
        return

    ctype = type(control).__name__
    name = getattr(control, "Name", "") or ""
    if filter_interactive and ctype not in _INTERACTIVE_TYPES:
        pass  # 但孩子可能可交互，继续遍历
    elif name or ctype:
        indent = "  " * depth
        auto_id = getattr(control, "AutomationId", "") or ""
        enabled = " [disabled]" if hasattr(control, "IsEnabled") and not control.IsEnabled() else ""
        lines.append(f"{indent}{ctype}: '{name}'" + (f" id={auto_id}" if auto_id else "") + enabled)

    try:
        for child in control.GetChildren():
            _walk_uia(child, lines, depth + 1, max_depth, filter_interactive)
    except Exception:
        pass
