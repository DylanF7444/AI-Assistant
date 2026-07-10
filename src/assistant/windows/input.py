from __future__ import annotations

import ctypes

from assistant.windows.application import WindowsApplicationAdapter


class WindowsInputAutomationAdapter:
    async def active_process_name(self) -> str | None:
        return await WindowsApplicationAdapter._active_process_name()

    async def focused_control_is_sensitive(self) -> bool:
        from ctypes import wintypes

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND),
                ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND),
                ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND),
                ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT),
            ]

        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetGUIThreadInfo(0, ctypes.byref(info)):
            return True
        if not info.hwndFocus:
            return True
        style = ctypes.windll.user32.GetWindowLongW(info.hwndFocus, -16)
        class_name = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(info.hwndFocus, class_name, len(class_name))
        return bool(style & 0x20) or class_name.value.casefold() in {
            "credential dialog xaml host",
            "passwordbox",
        }

    async def type_text(self, text: str) -> None:
        from ctypes import wintypes

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD),
                ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class INPUT_UNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

        inputs = []
        for char in text:
            code = ord(char)
            inputs.extend(
                [
                    INPUT(
                        1,
                        INPUT_UNION(ki=KEYBDINPUT(0, code, 0x0004, 0, None)),
                    ),
                    INPUT(
                        1,
                        INPUT_UNION(ki=KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, None)),
                    ),
                ]
            )
        if inputs:
            array_type = INPUT * len(inputs)
            values = array_type(*inputs)
            sent = ctypes.windll.user32.SendInput(len(values), values, ctypes.sizeof(INPUT))
            if sent != len(values):
                raise ctypes.WinError()

    async def click(self, x: int, y: int) -> None:
        if not ctypes.windll.user32.SetCursorPos(x, y):
            raise ctypes.WinError()
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
