from __future__ import annotations

import ctypes


class LocalWindowsSettingsAdapter:
    KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

    async def get_dark_mode(self) -> bool:
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
        except FileNotFoundError:
            return False

    async def set_dark_mode(self, enabled: bool) -> None:
        import winreg

        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, self.KEY) as key:
            value = 0 if enabled else 1
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, value)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, value)
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF,
            0x001A,
            0,
            "ImmersiveColorSet",
            0x0002,
            1000,
            None,
        )
