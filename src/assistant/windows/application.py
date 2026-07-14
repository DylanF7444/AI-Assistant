from __future__ import annotations

import csv
import ctypes
import os
import re
import subprocess
from pathlib import Path

from assistant.windows.interfaces import BrowserWindowInfo
from assistant.windows.process_names import (
    ASSISTANT_DEVELOPMENT_PROCESS_NAMES,
    ASSISTANT_WINDOW_TITLE,
    is_assistant_process_name,
    normalize_process_name,
)


def _win32_input_types():
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", wintypes.WPARAM),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]

    return KEYBDINPUT, INPUT_UNION, INPUT


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

    async def launch_with_args(self, registered_path: str, arguments: tuple[str, ...]) -> None:
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
            if cls._window_matches_target(hwnd, pid.value, pids, title_filtered_pids):
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
        return pid in title_filtered_pids and cls._window_title(hwnd) == ASSISTANT_WINDOW_TITLE

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
            dict.fromkeys(normalize_process_name(name) for name in process_names if name.strip())
        )
        pids_by_name = {
            pid: process_name for process_name in normalized for pid in cls._pids(process_name)
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
            dict.fromkeys(normalize_process_name(name) for name in process_names if name.strip())
        )
        pids_by_name = {
            pid: process_name for process_name in normalized for pid in cls._pids(process_name)
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

        ctypes.windll.user32.EnumDisplayMonitors(None, None, callback_type(callback), 0)
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

        KEYBDINPUT, INPUT_UNION, INPUT = _win32_input_types()

        key_codes: list[int] = []
        for key in keys:
            code = cls._KEY_CODES.get(key.casefold())
            if code is None:
                raise ValueError(f"Unsupported hotkey key: {key}")
            key_codes.append(code)
        inputs = [INPUT(1, INPUT_UNION(ki=KEYBDINPUT(code, 0, 0, 0, 0))) for code in key_codes]
        inputs.extend(
            INPUT(1, INPUT_UNION(ki=KEYBDINPUT(code, 0, 0x0002, 0, 0)))
            for code in reversed(key_codes)
        )
        values = (INPUT * len(inputs))(*inputs)
        send_input = ctypes.windll.user32.SendInput
        send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
        send_input.restype = wintypes.UINT
        sent = send_input(len(values), values, ctypes.sizeof(INPUT))
        if sent != len(values):
            raise ctypes.WinError()

    @staticmethod
    def _type_text(text: str) -> None:
        from ctypes import wintypes

        KEYBDINPUT, INPUT_UNION, INPUT = _win32_input_types()

        inputs = []
        for char in text:
            code = ord(char)
            inputs.extend(
                [
                    INPUT(
                        1,
                        INPUT_UNION(ki=KEYBDINPUT(0, code, 0x0004, 0, 0)),
                    ),
                    INPUT(
                        1,
                        INPUT_UNION(ki=KEYBDINPUT(0, code, 0x0004 | 0x0002, 0, 0)),
                    ),
                ]
            )
        if inputs:
            array_type = INPUT * len(inputs)
            values = array_type(*inputs)
            send_input = ctypes.windll.user32.SendInput
            send_input.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
            send_input.restype = wintypes.UINT
            sent = send_input(len(values), values, ctypes.sizeof(INPUT))
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
