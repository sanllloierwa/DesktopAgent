"""Screenshot capture — screen / window / region"""

from __future__ import annotations

import base64
import io
from typing import Any

from loguru import logger

from src.utils.windows_dpi import enable_per_monitor_dpi_awareness


async def capture_screenshot(region: tuple[int, int, int, int] | None = None) -> dict[str, Any]:
    """截取屏幕并返回 base64。

    Args:
        region: (left, top, right, bottom)，None 表示全屏

    Returns:
        {"success": True, "base64": "...", "summary": "..."}
    """
    try:
        from PIL import Image
        import mss

        enable_per_monitor_dpi_awareness()
        with mss.mss() as sct:
            if region:
                monitor = {
                    "left": region[0], "top": region[1],
                    "width": region[2] - region[0], "height": region[3] - region[1],
                }
                img_data = sct.grab(monitor)
            else:
                monitor = dict(sct.monitors[1])  # primary monitor
                img_data = sct.grab(monitor)

            img = Image.frombytes("RGB", img_data.size, img_data.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()

            return {
                "success": True,
                "base64": b64,
                "summary": b64,
                "size": img.size,
                "width": img.size[0],
                "height": img.size[1],
                "left": int(monitor["left"]),
                "top": int(monitor["top"]),
                "right": int(monitor["left"] + monitor["width"]),
                "bottom": int(monitor["top"] + monitor["height"]),
            }
    except ImportError as e:
        logger.warning(f"Screenshot deps missing: {e}")
        return {"success": False, "error": f"[ENV_ERR] Missing dependency: {e}"}
    except Exception as exc:
        logger.error(f"Screenshot failed: {exc}")
        return {"success": False, "error": str(exc)}
