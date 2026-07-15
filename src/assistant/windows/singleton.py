from __future__ import annotations

import ctypes
import sys


class SingleInstanceMutex:
    def __init__(self, handle: int | None = None) -> None:
        self._handle = handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and sys.platform == "win32":
            ctypes.windll.kernel32.CloseHandle(handle)

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup fallback
        self.close()


def acquire_single_instance(name: str = "LocalAssistant.Desktop") -> SingleInstanceMutex:
    if sys.platform != "win32":
        return SingleInstanceMutex()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    error = ctypes.get_last_error()
    if not handle:
        raise OSError(error, "Could not create application mutex")
    if error == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        raise RuntimeError("Local Assistant is already running for this Windows user")
    return SingleInstanceMutex(handle)
