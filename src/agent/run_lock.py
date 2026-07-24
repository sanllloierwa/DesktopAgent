"""Single-task execution lock.

The Windows named mutex prevents multiple UI windows/processes from running
independent AgentLoop instances against the same desktop and browser profile.
"""

from __future__ import annotations

import os
import threading


_fallback_lock = threading.Lock()


class TaskRunLock:
    def __init__(self, name: str = "Local\\DesktopAgent.SingleActiveTask") -> None:
        self.name = name
        self._handle = None
        self._fallback_acquired = False

    def acquire(self) -> bool:
        if os.name != "nt":
            self._fallback_acquired = _fallback_lock.acquire(blocking=False)
            return self._fallback_acquired

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR,
        ]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.CreateMutexW(None, True, self.name)
        if not handle:
            return False
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        self._handle = (kernel32, handle)
        return True

    def release(self) -> None:
        if self._handle is not None:
            kernel32, handle = self._handle
            kernel32.ReleaseMutex(handle)
            kernel32.CloseHandle(handle)
            self._handle = None
        if self._fallback_acquired:
            _fallback_lock.release()
            self._fallback_acquired = False
