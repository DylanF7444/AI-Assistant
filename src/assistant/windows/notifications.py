from __future__ import annotations

import ctypes


class WindowsNotificationAdapter:
    async def show(self, title: str, message: str) -> None:
        # MessageBox is a local, dependency-free fallback that remains visible
        # and accessible even when toast registration is unavailable.
        await __import__("asyncio").to_thread(
            ctypes.windll.user32.MessageBoxW, None, message, title, 0x40
        )
