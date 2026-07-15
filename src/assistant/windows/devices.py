from __future__ import annotations

import ctypes
import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from assistant.windows.media import WindowsMediaAdapter


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
    _APP_EXECUTABLE_EXCLUDE_MARKERS = (
        "crashpad",
        "installer",
        "setup",
        "unins",
        "uninstall",
        "updater",
        "vc_redist",
        "vcredist",
        "winsdksetup",
    )

    async def search_installed_apps(self, query: str | None = None, limit: int = 20) -> list[dict]:
        import winreg

        roots = (
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ),
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
                            install_location = self._registry_value(subkey, "InstallLocation")
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
            path for path in root.glob("*.exe") if cls._is_launchable_application_executable(path)
        ]
        if not candidates:
            return None
        words = {word for word in re.findall(r"[a-z0-9]+", name.casefold()) if len(word) > 2}
        scored = sorted(
            candidates,
            key=lambda path: (
                -sum(1 for word in words if word in path.stem.casefold()),
                len(path.name),
                path.name.casefold(),
            ),
        )
        return str(scored[0].resolve())

    @classmethod
    def _display_icon_executable(cls, display_icon: str | None) -> Path | None:
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
        if not cls._is_launchable_application_executable(path):
            return None
        return path.resolve()

    @classmethod
    def _is_launchable_application_executable(cls, path: Path) -> bool:
        name = path.name.casefold()
        return (
            path.suffix.casefold() == ".exe"
            and path.is_file()
            and name not in cls._APP_EXECUTABLE_EXCLUDE
            and not any(marker in name for marker in cls._APP_EXECUTABLE_EXCLUDE_MARKERS)
        )

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
            await self._powershell(f"{verb} -Name {json.dumps(adapter['Name'])} -Confirm:$false")
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
            "seconds_remaining": None
            if status.BatteryLifeTime == 0xFFFFFFFF
            else int(status.BatteryLifeTime),
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

    async def set_connected_device_enabled(self, instance_id: str, enabled: bool) -> None:
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

    async def _set_bluetooth_peripheral_enabled(self, device: dict, enabled: bool) -> None:
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
        output = await cls._powershell(f"{script} | ConvertTo-Json -Depth 4 -Compress")
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
        return [f"{chr(65 + index)}:\\" for index in range(26) if mask & (1 << index)]

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
            method = self._method(self.volume, 7, ctypes.HRESULT, ctypes.c_float, ctypes.c_void_p)
            result = method(self.volume, ctypes.c_float(scalar), None)
            if result:
                raise ctypes.WinError(result)
        finally:
            self._close()

    def set_muted(self, muted: bool) -> None:
        try:
            method = self._method(self.volume, 14, ctypes.HRESULT, ctypes.c_int, ctypes.c_void_p)
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
