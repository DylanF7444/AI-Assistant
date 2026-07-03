from __future__ import annotations

import os
import csv
import ctypes
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from assistant.windows.process_names import (
    ASSISTANT_DEVELOPMENT_PROCESS_NAMES,
    ASSISTANT_WINDOW_TITLE,
    is_assistant_process_name,
    normalize_process_name,
)


class ApplicationAdapter(Protocol):
    async def launch(self, registered_path: str) -> None: ...
    async def launch_with_args(
        self, registered_path: str, arguments: tuple[str, ...]
    ) -> None: ...
    async def open_url(self, url: str) -> None: ...
    def default_browser_path(self) -> str | None: ...
    async def focus(self, process_name: str) -> bool: ...
    async def minimize(self, process_name: str) -> bool: ...
    async def maximize(self, process_name: str) -> bool: ...
    async def minimize_focused(self) -> bool: ...
    async def maximize_focused(self) -> bool: ...
    async def close(self, process_name: str) -> bool: ...
    async def move_to_monitor(self, process_name: str, monitor: int) -> bool: ...
    async def move(self, process_name: str, x: int, y: int) -> bool: ...
    async def resize(self, process_name: str, width: int, height: int) -> bool: ...


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


@dataclass(frozen=True)
class BrowserWindowInfo:
    process_name: str
    private: bool
    title: str = ""


class ProcessAdapter(Protocol):
    async def list(self, name_filter: str | None = None) -> list[ProcessInfo]: ...
    async def terminate(self, pid: int) -> bool: ...


class InputAutomationAdapter(Protocol):
    async def active_process_name(self) -> str | None: ...
    async def focused_control_is_sensitive(self) -> bool: ...
    async def type_text(self, text: str) -> None: ...
    async def click(self, x: int, y: int) -> None: ...


class ContextNavigationAdapter(Protocol):
    async def active_process_name(self) -> str | None: ...
    async def first_visible_process(self, process_names: tuple[str, ...]) -> str | None: ...
    async def visible_browser_windows(
        self, process_names: tuple[str, ...]
    ) -> tuple[BrowserWindowInfo, ...]: ...
    async def focus(self, process_name: str) -> bool: ...
    async def hotkey(self, keys: tuple[str, ...]) -> None: ...
    async def type_text(self, text: str) -> None: ...


class WindowsSettingsAdapter(Protocol):
    async def get_dark_mode(self) -> bool: ...
    async def set_dark_mode(self, enabled: bool) -> None: ...


class WindowsDeviceAdapter(Protocol):
    async def search_installed_apps(
        self, query: str | None = None, limit: int = 20
    ) -> list[dict]: ...
    async def set_volume(self, level: int) -> None: ...
    async def change_volume(self, delta: int) -> None: ...
    async def mute_audio(self, muted: bool) -> None: ...
    async def set_brightness(self, level: int) -> None: ...
    async def change_brightness(self, delta: int) -> int: ...
    async def set_wifi_enabled(self, enabled: bool | None) -> bool: ...
    async def connect_wifi_network(self, ssid: str) -> None: ...
    async def disconnect_wifi_network(self, ssid: str | None = None) -> None: ...
    async def set_bluetooth_enabled(self, enabled: bool | None) -> bool: ...
    async def connect_bluetooth_device(self, device_name: str) -> None: ...
    async def disconnect_bluetooth_device(self, device_name: str) -> None: ...
    async def lock_device(self) -> None: ...
    async def restart_device(self) -> None: ...
    async def shutdown_device(self) -> None: ...
    async def battery_status(self) -> dict: ...
    async def storage_status(self, drive: str | None = None) -> list[dict]: ...
    async def performance_status(self) -> dict: ...
    async def connected_devices(self, limit: int = 50) -> list[dict]: ...
    async def set_connected_device_enabled(
        self, instance_id: str, enabled: bool
    ) -> None: ...


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
    _KEY_CODES = {
        **{chr(code + 96): 0x40 + code for code in range(1, 27)},
        "alt": 0x12,
        "ctrl": 0x11,
        "control": 0x11,
        "enter": 0x0D,
        "left": 0x25,
        "right": 0x27,
        "shift": 0x10,
        "tab": 0x09,
    }

    async def launch(self, registered_path: str) -> None:
        path = Path(registered_path)
        if not path.exists():
            raise FileNotFoundError(registered_path)
        os.startfile(path)  # type: ignore[attr-defined]

    async def launch_with_args(
        self, registered_path: str, arguments: tuple[str, ...]
    ) -> None:
        path = Path(registered_path)
        if not path.exists():
            raise FileNotFoundError(registered_path)
        subprocess.Popen(
            [str(path), *arguments],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    async def open_url(self, url: str) -> None:
        os.startfile(url)  # type: ignore[attr-defined]

    def default_browser_path(self) -> str | None:
        return self._default_browser_path()

    async def focus(self, process_name: str) -> bool:
        return self._show(process_name, 9, foreground=True)

    async def minimize(self, process_name: str) -> bool:
        return self._show(process_name, 6)

    async def maximize(self, process_name: str) -> bool:
        return self._show(process_name, 3, foreground=True)

    async def minimize_focused(self) -> bool:
        return self._show_focused(6)

    async def maximize_focused(self) -> bool:
        return self._show_focused(3, foreground=True)

    async def close(self, process_name: str) -> bool:
        return self._post_close(process_name)

    async def move_to_monitor(self, process_name: str, monitor: int) -> bool:
        import asyncio

        for _ in range(20):
            if self._move_to_monitor(process_name, monitor):
                return True
            await asyncio.sleep(0.25)
        return False

    async def move(self, process_name: str, x: int, y: int) -> bool:
        return self._move(process_name, x=x, y=y)

    async def resize(self, process_name: str, width: int, height: int) -> bool:
        return self._move(process_name, width=width, height=height)

    async def active_process_name(self) -> str | None:
        return await self._active_process_name()

    async def first_visible_process(self, process_names: tuple[str, ...]) -> str | None:
        return self._first_visible_process(process_names)

    async def visible_browser_windows(
        self, process_names: tuple[str, ...]
    ) -> tuple[BrowserWindowInfo, ...]:
        return self._visible_browser_windows(process_names)

    async def hotkey(self, keys: tuple[str, ...]) -> None:
        self._send_hotkey(keys)

    async def type_text(self, text: str) -> None:
        self._type_text(text)

    @classmethod
    async def _active_process_name(cls) -> str | None:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd:
            return None
        from ctypes import wintypes

        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return await cls._process_name_for_pid(pid.value)

    @staticmethod
    async def _process_name_for_pid(pid: int) -> str | None:
        completed = await __import__("asyncio").create_subprocess_exec(
            "tasklist",
            "/FI",
            f"PID eq {pid}",
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

    @staticmethod
    def _pids(process_name: str) -> set[int]:
        process_name = normalize_process_name(process_name)
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
        pids, title_filtered_pids = cls._target_pid_sets(process_name)
        handles: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _):
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            if cls._window_matches_target(
                hwnd, pid.value, pids, title_filtered_pids
            ):
                handles.append(hwnd)
            return True

        from ctypes import wintypes

        ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
        return handles

    @classmethod
    def _target_pid_sets(cls, process_name: str) -> tuple[set[int], set[int]]:
        normalized = normalize_process_name(process_name)
        pids = cls._pids(normalized)
        title_filtered_pids: set[int] = set()
        if is_assistant_process_name(normalized):
            for dev_process_name in ASSISTANT_DEVELOPMENT_PROCESS_NAMES:
                title_filtered_pids.update(cls._pids(dev_process_name))
        return pids, title_filtered_pids

    @classmethod
    def _window_matches_target(
        cls,
        hwnd: int,
        pid: int,
        pids: set[int],
        title_filtered_pids: set[int],
    ) -> bool:
        if pid in pids:
            return True
        return (
            pid in title_filtered_pids
            and cls._window_title(hwnd) == ASSISTANT_WINDOW_TITLE
        )

    @staticmethod
    def _window_title(hwnd: int) -> str:
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
        return buffer.value

    @classmethod
    def _first_visible_process(cls, process_names: tuple[str, ...]) -> str | None:
        normalized = tuple(
            dict.fromkeys(
                normalize_process_name(name)
                for name in process_names
                if name.strip()
            )
        )
        pids_by_name = {
            pid: process_name
            for process_name in normalized
            for pid in cls._pids(process_name)
        }
        if not pids_by_name:
            return None
        result: str | None = None
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _):
            nonlocal result
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids_by_name:
                result = pids_by_name[pid.value]
                return False
            return True

        from ctypes import wintypes

        ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
        return result

    @classmethod
    def _visible_browser_windows(
        cls, process_names: tuple[str, ...]
    ) -> tuple[BrowserWindowInfo, ...]:
        normalized = tuple(
            dict.fromkeys(
                normalize_process_name(name)
                for name in process_names
                if name.strip()
            )
        )
        pids_by_name = {
            pid: process_name
            for process_name in normalized
            for pid in cls._pids(process_name)
        }
        if not pids_by_name:
            return ()
        windows: list[BrowserWindowInfo] = []
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def callback(hwnd, _):
            if not ctypes.windll.user32.IsWindowVisible(hwnd):
                return True
            pid = wintypes.DWORD()
            ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            process_name = pids_by_name.get(pid.value)
            if process_name:
                title = cls._window_title(hwnd)
                windows.append(
                    BrowserWindowInfo(
                        process_name=process_name,
                        private=cls._browser_title_is_private(title),
                        title=title,
                    )
                )
            return True

        from ctypes import wintypes

        ctypes.windll.user32.EnumWindows(callback_type(callback), 0)
        return tuple(windows)

    @staticmethod
    def _browser_title_is_private(title: str) -> bool:
        return bool(
            re.search(
                r"\b(?:incognito|inprivate|private browsing|private window|private tab)\b",
                title,
                flags=re.IGNORECASE,
            )
        )

    @classmethod
    def _default_browser_path(cls) -> str | None:
        try:
            import winreg
        except ImportError:
            return None
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            ) as key:
                prog_id = str(winreg.QueryValueEx(key, "ProgId")[0]).strip()
        except OSError:
            prog_id = ""
        if not prog_id:
            return None
        command_subkey = rf"{prog_id}\shell\open\command"
        roots = (
            (winreg.HKEY_CURRENT_USER, r"Software\Classes"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Classes"),
            (winreg.HKEY_CLASSES_ROOT, ""),
        )
        for root, prefix in roots:
            key_path = rf"{prefix}\{command_subkey}" if prefix else command_subkey
            try:
                with winreg.OpenKey(root, key_path) as key:
                    command = str(winreg.QueryValueEx(key, "")[0]).strip()
            except OSError:
                continue
            path = cls._command_executable_path(command)
            if path:
                return path
        return None

    @staticmethod
    def _command_executable_path(command: str) -> str | None:
        command = command.strip()
        match = re.match(r'^"([^"]+\.exe)"', command, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.match(r"^(.+?\.exe)(?:\s|$)", command, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
        return None

    @classmethod
    def _show(cls, process_name: str, command: int, foreground: bool = False) -> bool:
        handles = cls._windows(process_name)
        for hwnd in handles:
            ctypes.windll.user32.ShowWindow(hwnd, command)
            if foreground:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        return bool(handles)

    @classmethod
    def _show_focused(cls, command: int, foreground: bool = False) -> bool:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        if not hwnd or not ctypes.windll.user32.IsWindowVisible(hwnd):
            return False
        ctypes.windll.user32.ShowWindow(hwnd, command)
        if foreground:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True

    @classmethod
    def _post_close(cls, process_name: str) -> bool:
        handles = cls._windows(process_name)
        for hwnd in handles:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0010, 0, 0)
        return bool(handles)

    @staticmethod
    def _monitor_work_areas() -> list[tuple[int, int, int, int]]:
        from ctypes import wintypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", wintypes.LONG),
                ("top", wintypes.LONG),
                ("right", wintypes.LONG),
                ("bottom", wintypes.LONG),
            ]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", RECT),
                ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD),
            ]

        work_areas: list[tuple[int, int, int, int]] = []
        callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_bool,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(RECT),
            ctypes.c_void_p,
        )

        def callback(hmonitor, _hdc, _rect, _data):
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(info)
            if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
                work_areas.append(
                    (
                        info.rcWork.left,
                        info.rcWork.top,
                        info.rcWork.right,
                        info.rcWork.bottom,
                    )
                )
            return True

        ctypes.windll.user32.EnumDisplayMonitors(
            None, None, callback_type(callback), 0
        )
        return work_areas

    @classmethod
    def _move_to_monitor(cls, process_name: str, monitor: int) -> bool:
        if monitor < 1:
            raise ValueError("Monitor numbers start at 1")
        work_areas = cls._monitor_work_areas()
        if monitor > len(work_areas):
            raise ValueError(f"Monitor {monitor} is not available")
        left, top, right, bottom = work_areas[monitor - 1]
        width = right - left
        height = bottom - top
        return cls._move(process_name, x=left, y=top, width=width, height=height)

    @classmethod
    def _send_hotkey(cls, keys: tuple[str, ...]) -> None:
        if not keys:
            raise ValueError("hotkey requires at least one key")
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

        key_codes: list[int] = []
        for key in keys:
            code = cls._KEY_CODES.get(key.casefold())
            if code is None:
                raise ValueError(f"Unsupported hotkey key: {key}")
            key_codes.append(code)
        inputs = [
            INPUT(1, INPUT_UNION(ki=KEYBDINPUT(code, 0, 0, 0, None)))
            for code in key_codes
        ]
        inputs.extend(
            INPUT(1, INPUT_UNION(ki=KEYBDINPUT(code, 0, 0x0002, 0, None)))
            for code in reversed(key_codes)
        )
        values = (INPUT * len(inputs))(*inputs)
        sent = ctypes.windll.user32.SendInput(
            len(values), values, ctypes.sizeof(INPUT)
        )
        if sent != len(values):
            raise ctypes.WinError()

    @staticmethod
    def _type_text(text: str) -> None:
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

    @classmethod
    def _move(
        cls,
        process_name: str,
        *,
        x: int | None = None,
        y: int | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> bool:
        from ctypes import wintypes

        handles = cls._windows(process_name)
        if not handles:
            return False

        rect = wintypes.RECT()
        for hwnd in handles:
            ctypes.windll.user32.ShowWindow(hwnd, 9)
            if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                continue
            next_x = rect.left if x is None else x
            next_y = rect.top if y is None else y
            next_width = rect.right - rect.left if width is None else width
            next_height = rect.bottom - rect.top if height is None else height
            ctypes.windll.user32.SetWindowPos(
                hwnd,
                None,
                int(next_x),
                int(next_y),
                int(next_width),
                int(next_height),
                0x0040,
            )
        return True


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


class LocalWindowsDeviceAdapter:
    _APP_EXECUTABLE_EXCLUDE = {
        "crashpad_handler.exe",
        "setup.exe",
        "unins000.exe",
        "uninstall.exe",
        "uninstaller.exe",
        "update.exe",
        "updater.exe",
    }

    async def search_installed_apps(
        self, query: str | None = None, limit: int = 20
    ) -> list[dict]:
        import winreg

        roots = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        )
        needle = (query or "").casefold().strip()
        seen: set[str] = set()
        apps: list[dict] = []
        for hive, key_path in roots:
            try:
                key = winreg.OpenKey(hive, key_path)
            except OSError:
                continue
            with key:
                for index in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        subkey_name = winreg.EnumKey(key, index)
                        with winreg.OpenKey(key, subkey_name) as subkey:
                            name = self._registry_value(subkey, "DisplayName")
                            if not name:
                                continue
                            if needle and needle not in name.casefold():
                                continue
                            identity = name.casefold()
                            if identity in seen:
                                continue
                            seen.add(identity)
                            install_location = self._registry_value(
                                subkey, "InstallLocation"
                            )
                            display_icon = self._registry_value(subkey, "DisplayIcon")
                            apps.append(
                                {
                                    "name": name,
                                    "publisher": self._registry_value(subkey, "Publisher"),
                                    "version": self._registry_value(subkey, "DisplayVersion"),
                                    "install_location": install_location,
                                    "display_icon": display_icon,
                                    "executable_path": self._application_executable_path(
                                        name, display_icon, install_location
                                    ),
                                }
                            )
                    except OSError:
                        continue
                    if len(apps) >= limit:
                        return apps
        return sorted(apps, key=lambda item: item["name"].casefold())[:limit]

    @classmethod
    def _application_executable_path(
        cls,
        name: str,
        display_icon: str | None,
        install_location: str | None,
    ) -> str | None:
        display_icon_path = cls._display_icon_executable(display_icon)
        if display_icon_path:
            return str(display_icon_path)
        if not install_location:
            return None
        root = Path(install_location).expanduser()
        if not root.is_dir():
            return None
        candidates = [
            path
            for path in root.glob("*.exe")
            if path.name.casefold() not in cls._APP_EXECUTABLE_EXCLUDE
        ]
        if not candidates:
            return None
        words = {
            word
            for word in re.findall(r"[a-z0-9]+", name.casefold())
            if len(word) > 2
        }
        scored = sorted(
            candidates,
            key=lambda path: (
                -sum(1 for word in words if word in path.stem.casefold()),
                len(path.name),
                path.name.casefold(),
            ),
        )
        return str(scored[0].resolve())

    @staticmethod
    def _display_icon_executable(display_icon: str | None) -> Path | None:
        if not display_icon:
            return None
        value = display_icon.strip()
        if value.startswith('"'):
            match = re.match(r'^"([^"]+)"', value)
            candidate = match.group(1) if match else value.strip('"')
        else:
            candidate = value.split(",", 1)[0].strip()
            if ".exe" in candidate.casefold():
                candidate = candidate[: candidate.casefold().find(".exe") + 4]
        path = Path(candidate).expanduser()
        if path.suffix.casefold() != ".exe" or not path.is_file():
            return None
        return path.resolve()

    async def set_volume(self, level: int) -> None:
        self._audio_endpoint().set_volume(max(0, min(100, level)) / 100)

    async def change_volume(self, delta: int) -> None:
        action = "volume_up" if delta > 0 else "volume_down"
        steps = max(1, min(50, round(abs(delta) / 2)))
        media = WindowsMediaAdapter()
        for _ in range(steps):
            await media.command(action)

    async def mute_audio(self, muted: bool) -> None:
        self._audio_endpoint().set_muted(muted)

    async def set_brightness(self, level: int) -> None:
        await self._powershell(
            "$m = Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods; "
            f"$m.WmiSetBrightness(1,{max(0, min(100, level))}) | Out-Null"
        )

    async def change_brightness(self, delta: int) -> int:
        current = await self._current_brightness()
        level = max(0, min(100, current + delta))
        await self.set_brightness(level)
        return level

    async def set_wifi_enabled(self, enabled: bool | None) -> bool:
        adapters = await self._json_powershell(
            "Get-NetAdapter | Where-Object { $_.NdisPhysicalMedium -eq 9 -or "
            "$_.Name -match 'wi-?fi|wireless|wlan' } | "
            "Select-Object -First 1 -Property Name,Status"
        )
        adapter = self._first(adapters)
        if not adapter or not adapter.get("Name"):
            raise RuntimeError("No Wi-Fi adapter was found")
        target = (adapter.get("Status") != "Up") if enabled is None else enabled
        verb = "Enable-NetAdapter" if target else "Disable-NetAdapter"
        try:
            await self._powershell(
                f"{verb} -Name {json.dumps(adapter['Name'])} -Confirm:$false"
            )
        except RuntimeError as exc:
            message = str(exc)
            if any(
                phrase in message.casefold()
                for phrase in ("access is denied", "administrator", "privilege")
            ):
                raise RuntimeError(
                    "Changing Wi-Fi requires administrator privileges. "
                    "Restart the assistant as administrator, then try again."
                ) from exc
            raise
        return bool(target)

    async def connect_wifi_network(self, ssid: str) -> None:
        adapter = await self._wifi_adapter()
        await self._run(
            "netsh",
            "wlan",
            "connect",
            f"name={ssid}",
            f"ssid={ssid}",
            f"interface={adapter['Name']}",
        )

    async def disconnect_wifi_network(self, ssid: str | None = None) -> None:
        adapter = await self._wifi_adapter()
        if ssid:
            current = await self._current_wifi_ssid(adapter["Name"])
            if current.casefold() != ssid.casefold():
                raise RuntimeError(f"Wi-Fi is not connected to {ssid}")
        await self._run("netsh", "wlan", "disconnect", f"interface={adapter['Name']}")

    async def set_bluetooth_enabled(self, enabled: bool | None) -> bool:
        devices = await self._json_powershell(
            "Get-PnpDevice -Class Bluetooth | "
            "Where-Object { $_.Status -in @('OK','Error') } | "
            "Select-Object -Property FriendlyName,InstanceId,Status"
        )
        rows = devices if isinstance(devices, list) else ([devices] if devices else [])
        candidates = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("InstanceId")
            and not str(row.get("InstanceId")).upper().startswith("BTHENUM\\")
        ]
        device = candidates[0] if candidates else None
        if not device or not device.get("InstanceId"):
            raise RuntimeError("No controllable Bluetooth radio was found")
        target = (device.get("Status") != "OK") if enabled is None else enabled
        verb = "Enable-PnpDevice" if target else "Disable-PnpDevice"
        try:
            await self._powershell(
                f"{verb} -InstanceId {self._ps_single_quoted(str(device['InstanceId']))} -Confirm:$false"
            )
        except RuntimeError as exc:
            message = str(exc)
            if any(
                phrase in message.casefold()
                for phrase in ("access is denied", "administrator", "privilege")
            ):
                raise RuntimeError(
                    "Changing Bluetooth requires administrator privileges. "
                    "Restart the assistant as administrator, then try again."
                ) from exc
            raise
        return bool(target)

    async def connect_bluetooth_device(self, device_name: str) -> None:
        device = await self._bluetooth_peripheral(device_name)
        await self._set_bluetooth_peripheral_enabled(device, True)

    async def disconnect_bluetooth_device(self, device_name: str) -> None:
        device = await self._bluetooth_peripheral(device_name)
        await self._set_bluetooth_peripheral_enabled(device, False)

    async def lock_device(self) -> None:
        if not ctypes.windll.user32.LockWorkStation():
            raise ctypes.WinError()

    async def restart_device(self) -> None:
        await self._run("shutdown", "/r", "/t", "0")

    async def shutdown_device(self) -> None:
        await self._run("shutdown", "/s", "/t", "0")

    async def battery_status(self) -> dict:
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_byte),
                ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte),
                ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        status = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
            raise ctypes.WinError()
        return {
            "percent": None if status.BatteryLifePercent == 255 else int(status.BatteryLifePercent),
            "plugged_in": None if status.ACLineStatus == 255 else bool(status.ACLineStatus),
            "seconds_remaining": None if status.BatteryLifeTime == 0xFFFFFFFF else int(status.BatteryLifeTime),
            "charging": bool(status.BatteryFlag & 8),
            "battery_present": not bool(status.BatteryFlag & 128),
        }

    async def storage_status(self, drive: str | None = None) -> list[dict]:
        drives = [drive] if drive else self._logical_drives()
        values = []
        for item in drives:
            root = item if item.endswith("\\") else f"{item.rstrip(':')}:\\"
            try:
                usage = shutil.disk_usage(root)
            except OSError:
                continue
            values.append(
                {
                    "drive": root,
                    "total_bytes": usage.total,
                    "used_bytes": usage.used,
                    "free_bytes": usage.free,
                    "percent_used": round((usage.used / usage.total) * 100, 1)
                    if usage.total
                    else 0,
                }
            )
        return values

    async def performance_status(self) -> dict:
        memory = self._memory_status()
        cpu_percent = await self._cpu_percent()
        return {"cpu_percent": cpu_percent, "memory": memory}

    async def connected_devices(self, limit: int = 50) -> list[dict]:
        devices = await self._json_powershell(
            "Get-PnpDevice -PresentOnly | "
            "Where-Object { $_.FriendlyName } | "
            "Select-Object -First "
            f"{max(1, min(200, limit))} "
            "-Property FriendlyName,Class,Status,InstanceId"
        )
        rows = devices if isinstance(devices, list) else ([devices] if devices else [])
        return [
            {
                "name": row.get("FriendlyName"),
                "class": row.get("Class"),
                "status": row.get("Status"),
                "instance_id": row.get("InstanceId"),
            }
            for row in rows
            if isinstance(row, dict)
        ]

    async def set_connected_device_enabled(
        self, instance_id: str, enabled: bool
    ) -> None:
        verb = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
        await self._powershell(
            f"{verb} -InstanceId {self._ps_single_quoted(instance_id)} -Confirm:$false"
        )

    async def _wifi_adapter(self) -> dict:
        adapters = await self._json_powershell(
            "Get-NetAdapter | Where-Object { $_.NdisPhysicalMedium -eq 9 -or "
            "$_.Name -match 'wi-?fi|wireless|wlan' } | "
            "Select-Object -First 1 -Property Name,Status"
        )
        adapter = self._first(adapters)
        if not adapter or not adapter.get("Name"):
            raise RuntimeError("No Wi-Fi adapter was found")
        return adapter

    async def _current_wifi_ssid(self, interface_name: str) -> str:
        output = await self._run("netsh", "wlan", "show", "interfaces")
        in_target_interface = False
        current_ssid = ""
        for line in output.splitlines():
            key, _, value = line.partition(":")
            key = key.strip().casefold()
            value = value.strip()
            if key == "name":
                in_target_interface = value.casefold() == interface_name.casefold()
            elif in_target_interface and key == "ssid" and value:
                current_ssid = value
                break
        return current_ssid

    async def _bluetooth_peripheral(self, device_name: str) -> dict:
        candidates = await self._bluetooth_peripheral_matches(device_name)
        if not candidates and self._is_audio_device_alias(device_name):
            for resolved_name in await self._bluetooth_audio_device_names(device_name):
                candidates = await self._bluetooth_peripheral_matches(resolved_name)
                if candidates:
                    break
        if not candidates:
            raise RuntimeError(f"No paired Bluetooth device matched {device_name}")
        exact = [
            row
            for row in candidates
            if str(row.get("FriendlyName", "")).casefold() == device_name.casefold()
        ]
        return exact[0] if exact else candidates[0]

    async def _bluetooth_peripheral_matches(self, device_name: str) -> list[dict]:
        devices = await self._json_powershell(
            "Get-PnpDevice -Class Bluetooth | "
            "Where-Object { $_.FriendlyName -and $_.FriendlyName -like "
            f"{self._ps_single_quoted('*' + device_name + '*')} }} | "
            "Select-Object -Property FriendlyName,InstanceId,Status"
        )
        rows = devices if isinstance(devices, list) else ([devices] if devices else [])
        return [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("InstanceId")
            and str(row.get("InstanceId")).upper().startswith("BTHENUM\\")
        ]

    async def _bluetooth_audio_device_names(self, alias: str) -> list[str]:
        devices = await self._json_powershell(
            "Get-PnpDevice -PresentOnly | "
            "Where-Object { $_.FriendlyName } | "
            "Select-Object -Property FriendlyName,Class,Status,InstanceId"
        )
        rows = devices if isinstance(devices, list) else ([devices] if devices else [])
        names: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("FriendlyName") or "").strip()
            if not self._looks_like_audio_endpoint(name, str(row.get("Class") or "")):
                continue
            if not self._audio_alias_matches(alias, name):
                continue
            for candidate in self._audio_device_search_names(name):
                folded = candidate.casefold()
                if folded and folded not in {item.casefold() for item in names}:
                    names.append(candidate)
        return names

    @staticmethod
    def _is_audio_device_alias(value: str) -> bool:
        return value.casefold().strip() in {
            "headphone",
            "headphones",
            "headset",
            "headsets",
            "earbud",
            "earbuds",
            "speaker",
            "speakers",
        }

    @classmethod
    def _audio_alias_matches(cls, alias: str, name: str) -> bool:
        if not cls._is_audio_device_alias(alias):
            return alias.casefold() in name.casefold()
        folded_name = name.casefold()
        return any(
            word in folded_name
            for word in (
                "headphone",
                "headphones",
                "headset",
                "earbud",
                "earbuds",
                "hands-free",
                "stereo",
                "speaker",
                "audio",
            )
        )

    @staticmethod
    def _looks_like_audio_endpoint(name: str, device_class: str) -> bool:
        folded = f"{device_class} {name}".casefold()
        return any(
            word in folded
            for word in (
                "audio",
                "media",
                "headphone",
                "headphones",
                "headset",
                "earbud",
                "earbuds",
                "hands-free",
                "stereo",
                "speaker",
            )
        )

    @staticmethod
    def _audio_device_search_names(name: str) -> list[str]:
        candidates: list[str] = []
        for match in re.finditer(r"\(([^()]+)\)", name):
            value = match.group(1).strip()
            value = re.sub(
                r"\b(?:stereo|hands-free|headphones?|headsets?|speakers?|audio)\b",
                "",
                value,
                flags=re.IGNORECASE,
            ).strip(" -")
            if value:
                candidates.append(value)
        cleaned = re.sub(
            r"\b(?:stereo|hands-free|headphones?|headsets?|speakers?|audio)\b",
            "",
            name,
            flags=re.IGNORECASE,
        ).strip(" -()")
        if cleaned:
            candidates.append(cleaned)
        return candidates

    async def _set_bluetooth_peripheral_enabled(
        self, device: dict, enabled: bool
    ) -> None:
        verb = "Enable-PnpDevice" if enabled else "Disable-PnpDevice"
        try:
            await self._powershell(
                f"{verb} -InstanceId {self._ps_single_quoted(str(device['InstanceId']))} -Confirm:$false"
            )
        except RuntimeError as exc:
            message = str(exc)
            if any(
                phrase in message.casefold()
                for phrase in ("access is denied", "administrator", "privilege")
            ):
                action = "Connecting to" if enabled else "Disconnecting from"
                raise RuntimeError(
                    f"{action} Bluetooth devices requires administrator privileges. "
                    "Restart the assistant as administrator, then try again."
                ) from exc
            raise

    @staticmethod
    def _ps_single_quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _registry_value(key, name: str) -> str | None:
        try:
            value, _ = __import__("winreg").QueryValueEx(key, name)
        except OSError:
            return None
        return str(value).strip() or None

    @staticmethod
    async def _run(*args: str) -> str:
        completed = await __import__("asyncio").create_subprocess_exec(
            *args,
            stdout=__import__("asyncio").subprocess.PIPE,
            stderr=__import__("asyncio").subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout, stderr = await completed.communicate()
        if completed.returncode:
            raise RuntimeError(stderr.decode(errors="replace").strip() or args[0])
        return stdout.decode(errors="replace")

    @classmethod
    async def _powershell(cls, script: str) -> str:
        return await cls._run(
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        )

    @classmethod
    async def _json_powershell(cls, script: str):
        output = await cls._powershell(
            f"{script} | ConvertTo-Json -Depth 4 -Compress"
        )
        return json.loads(output) if output.strip() else None

    @staticmethod
    def _first(value):
        if isinstance(value, list):
            return value[0] if value else None
        return value

    @classmethod
    async def _current_brightness(cls) -> int:
        value = await cls._json_powershell(
            "Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness | "
            "Select-Object -First 1 -ExpandProperty CurrentBrightness"
        )
        if not isinstance(value, int):
            raise RuntimeError("Screen brightness is not available")
        return value

    @staticmethod
    def _logical_drives() -> list[str]:
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [
            f"{chr(65 + index)}:\\"
            for index in range(26)
            if mask & (1 << index)
        ]

    @staticmethod
    def _memory_status() -> dict:
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MEMORYSTATUSEX()
        status.dwLength = ctypes.sizeof(status)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise ctypes.WinError()
        return {
            "percent_used": int(status.dwMemoryLoad),
            "total_bytes": int(status.ullTotalPhys),
            "available_bytes": int(status.ullAvailPhys),
        }

    @staticmethod
    async def _cpu_percent() -> float:
        import asyncio

        first = LocalWindowsDeviceAdapter._system_times()
        await asyncio.sleep(0.1)
        second = LocalWindowsDeviceAdapter._system_times()
        idle = second[0] - first[0]
        total = sum(second) - sum(first)
        return round(max(0.0, min(100.0, 100.0 * (1 - idle / total))), 1) if total else 0.0

    @staticmethod
    def _system_times() -> tuple[int, int, int]:
        from ctypes import wintypes

        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise ctypes.WinError()

        def value(filetime) -> int:
            return (filetime.dwHighDateTime << 32) | filetime.dwLowDateTime

        return value(idle), value(kernel), value(user)

    @staticmethod
    def _audio_endpoint():
        return _AudioEndpoint()


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, value: str) -> None:
        guid = uuid.UUID(value)
        data = guid.bytes_le
        super().__init__(
            int.from_bytes(data[0:4], "little"),
            int.from_bytes(data[4:6], "little"),
            int.from_bytes(data[6:8], "little"),
            (ctypes.c_ubyte * 8).from_buffer_copy(data[8:16]),
        )


class _AudioEndpoint:
    CLSCTX_ALL = 23
    CLSID_MM_DEVICE_ENUMERATOR = _GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    IID_IMM_DEVICE_ENUMERATOR = _GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    IID_IAUDIO_ENDPOINT_VOLUME = _GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")

    def __init__(self) -> None:
        ctypes.windll.ole32.CoInitialize(None)
        self.enumerator = ctypes.c_void_p()
        result = ctypes.windll.ole32.CoCreateInstance(
            ctypes.byref(self.CLSID_MM_DEVICE_ENUMERATOR),
            None,
            self.CLSCTX_ALL,
            ctypes.byref(self.IID_IMM_DEVICE_ENUMERATOR),
            ctypes.byref(self.enumerator),
        )
        if result:
            raise ctypes.WinError(result)
        self.device = ctypes.c_void_p()
        self.volume = ctypes.c_void_p()
        try:
            self._get_default_device()
            self._activate_volume()
        except Exception:
            self._release(self.volume)
            self._release(self.device)
            self._release(self.enumerator)
            raise

    def set_volume(self, scalar: float) -> None:
        try:
            method = self._method(
                self.volume, 7, ctypes.HRESULT, ctypes.c_float, ctypes.c_void_p
            )
            result = method(self.volume, ctypes.c_float(scalar), None)
            if result:
                raise ctypes.WinError(result)
        finally:
            self._close()

    def set_muted(self, muted: bool) -> None:
        try:
            method = self._method(
                self.volume, 14, ctypes.HRESULT, ctypes.c_int, ctypes.c_void_p
            )
            result = method(self.volume, int(muted), None)
            if result:
                raise ctypes.WinError(result)
        finally:
            self._close()

    def _get_default_device(self) -> None:
        method = self._method(
            self.enumerator,
            4,
            ctypes.HRESULT,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_void_p),
        )
        result = method(self.enumerator, 0, 0, ctypes.byref(self.device))
        if result:
            raise ctypes.WinError(result)

    def _activate_volume(self) -> None:
        method = self._method(
            self.device,
            3,
            ctypes.HRESULT,
            ctypes.POINTER(_GUID),
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )
        result = method(
            self.device,
            ctypes.byref(self.IID_IAUDIO_ENDPOINT_VOLUME),
            self.CLSCTX_ALL,
            None,
            ctypes.byref(self.volume),
        )
        if result:
            raise ctypes.WinError(result)

    @staticmethod
    def _method(pointer, index: int, result_type, *arg_types):
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return ctypes.WINFUNCTYPE(result_type, ctypes.c_void_p, *arg_types)(vtable[index])

    @staticmethod
    def _release(pointer) -> None:
        if pointer:
            method = _AudioEndpoint._method(pointer, 2, ctypes.c_ulong)
            method(pointer)

    def _close(self) -> None:
        self._release(self.volume)
        self._release(self.device)
        self._release(self.enumerator)


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
