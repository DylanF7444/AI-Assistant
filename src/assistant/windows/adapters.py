from __future__ import annotations

import os
import csv
import ctypes
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ApplicationAdapter(Protocol):
    async def launch(self, registered_path: str) -> None: ...
    async def focus(self, process_name: str) -> bool: ...
    async def minimize(self, process_name: str) -> bool: ...
    async def maximize(self, process_name: str) -> bool: ...
    async def close(self, process_name: str) -> bool: ...


class MediaAdapter(Protocol):
    async def command(self, action: str) -> None: ...


class NotificationAdapter(Protocol):
    async def show(self, title: str, message: str) -> None: ...


@dataclass(frozen=True)
class ProcessInfo:
    image_name: str
    pid: int
    session_name: str
    memory: str


class ProcessAdapter(Protocol):
    async def list(self, name_filter: str | None = None) -> list[ProcessInfo]: ...
    async def terminate(self, pid: int) -> bool: ...


class InputAutomationAdapter(Protocol):
    async def active_process_name(self) -> str | None: ...
    async def focused_control_is_sensitive(self) -> bool: ...
    async def type_text(self, text: str) -> None: ...
    async def click(self, x: int, y: int) -> None: ...


class WindowsSettingsAdapter(Protocol):
    async def get_dark_mode(self) -> bool: ...
    async def set_dark_mode(self, enabled: bool) -> None: ...


class ClipboardAdapter(Protocol):
    async def read(self) -> str: ...
    async def write(self, text: str) -> None: ...


class FileSystemAdapter(Protocol):
    async def search(self, root: Path, query: str, limit: int) -> list[Path]: ...
    async def copy(self, source: Path, destination: Path) -> Path: ...
    async def move(self, source: Path, destination: Path) -> Path: ...
    async def delete_to_recycle_bin(self, path: Path) -> None: ...
    async def open(self, path: Path) -> None: ...


class WindowsApplicationAdapter:
    async def launch(self, registered_path: str) -> None:
        path = Path(registered_path)
        if not path.exists():
            raise FileNotFoundError(registered_path)
        os.startfile(path)  # type: ignore[attr-defined]

    async def focus(self, process_name: str) -> bool:
        return self._show(process_name, 9, foreground=True)

    async def minimize(self, process_name: str) -> bool:
        return self._show(process_name, 6)

    async def maximize(self, process_name: str) -> bool:
        return self._show(process_name, 3, foreground=True)

    async def close(self, process_name: str) -> bool:
        return self._post_close(process_name)

    @staticmethod
    def _pids(process_name: str) -> set[int]:
        completed = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {process_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            check=False,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        pids = set()
        for row in csv.reader(completed.stdout.splitlines()):
            if len(row) >= 2 and row[1].isdigit():
                pids.add(int(row[1]))
        return pids

    @classmethod
    def _windows(cls, process_name: str) -> list[int]:
        pids = cls._pids(process_name)
        handles: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _):
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and ctypes.windll.user32.IsWindowVisible(hwnd):
                handles.append(hwnd)
            return True

        from ctypes import wintypes

        ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
        return handles

    @classmethod
    def _show(cls, process_name: str, command: int, foreground: bool = False) -> bool:
        handles = cls._windows(process_name)
        for hwnd in handles:
            ctypes.windll.user32.ShowWindow(hwnd, command)
            if foreground:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        return bool(handles)

    @classmethod
    def _post_close(cls, process_name: str) -> bool:
        handles = cls._windows(process_name)
        for hwnd in handles:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return bool(handles)


class LocalFileSystemAdapter:
    async def search(self, root: Path, query: str, limit: int) -> list[Path]:
        if not root.is_dir():
            raise NotADirectoryError(root)
        needle = query.casefold()
        matches: list[Path] = []
        for path in root.rglob("*"):
            if needle in path.name.casefold():
                matches.append(path)
                if len(matches) >= limit:
                    break
        return matches

    async def copy(self, source: Path, destination: Path) -> Path:
        return Path(shutil.copy2(source, destination))

    async def move(self, source: Path, destination: Path) -> Path:
        return Path(shutil.move(source, destination))

    async def delete_to_recycle_bin(self, path: Path) -> None:
        try:
            from send2trash import send2trash
        except ImportError as exc:
            raise RuntimeError(
                "Safe deletion requires the optional send2trash package"
            ) from exc
        send2trash(str(path))

    async def open(self, path: Path) -> None:
        if not path.exists():
            raise FileNotFoundError(path)
        os.startfile(path)  # type: ignore[attr-defined]


class WindowsClipboardAdapter:
    async def read(self) -> str:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return root.clipboard_get()
        finally:
            root.destroy()

    async def write(self, text: str) -> None:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
            root.update()
        finally:
            root.destroy()


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
        import ctypes

        if action not in self.KEYS:
            raise ValueError(f"Unsupported media action: {action}")
        key = self.KEYS[action]
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        ctypes.windll.user32.keybd_event(key, 0, 2, 0)


class WindowsNotificationAdapter:
    async def show(self, title: str, message: str) -> None:
        # MessageBox is a local, dependency-free fallback that remains visible
        # and accessible even when toast registration is unavailable.
        await __import__("asyncio").to_thread(
            ctypes.windll.user32.MessageBoxW, None, message, title, 0x40
        )


class WindowsProcessAdapter:
    async def list(self, name_filter: str | None = None) -> list[ProcessInfo]:
        completed = await __import__("asyncio").create_subprocess_exec(
            "tasklist",
            "/FO",
            "CSV",
            "/NH",
            stdout=__import__("asyncio").subprocess.PIPE,
            stderr=__import__("asyncio").subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, _ = await completed.communicate()
        rows = []
        for row in csv.reader(stdout.decode(errors="replace").splitlines()):
            if len(row) < 5 or not row[1].isdigit():
                continue
            info = ProcessInfo(row[0], int(row[1]), row[2], row[4])
            if not name_filter or name_filter.casefold() in info.image_name.casefold():
                rows.append(info)
        return rows

    async def terminate(self, pid: int) -> bool:
        completed = await __import__("asyncio").create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            stdout=__import__("asyncio").subprocess.DEVNULL,
            stderr=__import__("asyncio").subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return await completed.wait() == 0


class WindowsInputAutomationAdapter:
    async def active_process_name(self) -> str | None:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        from ctypes import wintypes

        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        completed = await __import__("asyncio").create_subprocess_exec(
            "tasklist",
            "/FI",
            f"PID eq {pid.value}",
            "/FO",
            "CSV",
            "/NH",
            stdout=__import__("asyncio").subprocess.PIPE,
            stderr=__import__("asyncio").subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, _ = await completed.communicate()
        rows = list(csv.reader(stdout.decode(errors="replace").splitlines()))
        return rows[0][0] if rows and len(rows[0]) >= 2 else None

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
        ctypes.windll.user32.GetClassNameW(
            info.hwndFocus, class_name, len(class_name)
        )
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
                        INPUT_UNION(
                            ki=KEYBDINPUT(0, code, 0x0004, 0, None)
                        ),
                    ),
                    INPUT(
                        1,
                        INPUT_UNION(
                            ki=KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, None)
                        ),
                    ),
                ]
            )
        if inputs:
            array_type = INPUT * len(inputs)
            values = array_type(*inputs)
            sent = ctypes.windll.user32.SendInput(
                len(values), values, ctypes.sizeof(INPUT)
            )
            if sent != len(values):
                raise ctypes.WinError()

    async def click(self, x: int, y: int) -> None:
        if not ctypes.windll.user32.SetCursorPos(x, y):
            raise ctypes.WinError()
        ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
        ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)


class LocalWindowsSettingsAdapter:
    KEY = (
        r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    )

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
