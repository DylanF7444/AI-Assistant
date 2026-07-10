from __future__ import annotations

import ctypes
from ctypes import wintypes


class WindowsJob:
    """Kill-on-close Windows Job Object with process, memory, and CPU limits."""

    def __init__(self, process_handle: int, *, memory_mb: int, cpu_seconds: int) -> None:
        if not hasattr(ctypes, "windll"):
            self.handle = None
            return
        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError()

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        limits = EXTENDED_LIMITS()
        limits.BasicLimitInformation.PerProcessUserTimeLimit = cpu_seconds * 10_000_000
        limits.BasicLimitInformation.ActiveProcessLimit = 1
        limits.ProcessMemoryLimit = memory_mb * 1024 * 1024
        limits.BasicLimitInformation.LimitFlags = (
            0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | 0x00000008  # JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | 0x00000100  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | 0x00000002  # JOB_OBJECT_LIMIT_PROCESS_TIME
        )
        if not kernel32.SetInformationJobObject(
            self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise ctypes.WinError()
        if not kernel32.AssignProcessToJobObject(self.handle, wintypes.HANDLE(process_handle)):
            self.close()
            raise ctypes.WinError()

    def close(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
