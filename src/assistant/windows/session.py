from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


def is_session_unlocked() -> bool:
    """Best-effort check for an interactive Windows desktop.

    A failed or inaccessible desktop is treated as locked. Non-Windows test and
    development environments do not have a Windows session to gate.
    """
    if sys.platform != "win32":
        return True
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.SwitchDesktop.argtypes = (wintypes.HANDLE,)
    user32.SwitchDesktop.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = (wintypes.HANDLE,)
    user32.CloseDesktop.restype = wintypes.BOOL
    desktop = user32.OpenInputDesktop(0, False, 0x0100)
    if not desktop:
        return False
    try:
        return bool(user32.SwitchDesktop(desktop))
    finally:
        user32.CloseDesktop(desktop)
