from __future__ import annotations

import ctypes


class WindowsMediaAdapter:
    KEYS = {
        "play_pause": 0xB3,
        "next": 0xB0,
        "previous": 0xB1,
        "stop": 0xB2,
        "volume_mute": 0xAD,
        "volume_down": 0xAE,
        "volume_up": 0xAF,
    }

    async def command(self, action: str) -> None:

        if action not in self.KEYS:
            raise ValueError(f"Unsupported media action: {action}")
        key = self.KEYS[action]
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, 2, 0)
