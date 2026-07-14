"""Best-effort Windows DPI awareness for pixel-accurate screenshots and input."""

from __future__ import annotations

import ctypes


_DPI_AWARENESS_SET = False


def enable_per_monitor_dpi_awareness() -> None:
    """Use physical pixels when possible; harmlessly no-op if already configured."""
    global _DPI_AWARENESS_SET
    if _DPI_AWARENESS_SET:
        return
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    _DPI_AWARENESS_SET = True
