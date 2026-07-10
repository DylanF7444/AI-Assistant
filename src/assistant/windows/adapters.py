from assistant.windows.application import WindowsApplicationAdapter
from assistant.windows.clipboard import WindowsClipboardAdapter
from assistant.windows.devices import LocalWindowsDeviceAdapter
from assistant.windows.filesystem import LocalFileSystemAdapter
from assistant.windows.input import WindowsInputAutomationAdapter
from assistant.windows.interfaces import (
    ApplicationAdapter,
    BrowserWindowInfo,
    ClipboardAdapter,
    ContextNavigationAdapter,
    FileSystemAdapter,
    InputAutomationAdapter,
    MediaAdapter,
    NotificationAdapter,
    ProcessAdapter,
    ProcessInfo,
    WindowsDeviceAdapter,
    WindowsSettingsAdapter,
)
from assistant.windows.media import WindowsMediaAdapter
from assistant.windows.notifications import WindowsNotificationAdapter
from assistant.windows.processes import WindowsProcessAdapter
from assistant.windows.settings import LocalWindowsSettingsAdapter

__all__ = [
    "ApplicationAdapter",
    "BrowserWindowInfo",
    "ClipboardAdapter",
    "ContextNavigationAdapter",
    "FileSystemAdapter",
    "InputAutomationAdapter",
    "LocalFileSystemAdapter",
    "LocalWindowsDeviceAdapter",
    "LocalWindowsSettingsAdapter",
    "MediaAdapter",
    "NotificationAdapter",
    "ProcessAdapter",
    "ProcessInfo",
    "WindowsApplicationAdapter",
    "WindowsClipboardAdapter",
    "WindowsDeviceAdapter",
    "WindowsInputAutomationAdapter",
    "WindowsMediaAdapter",
    "WindowsNotificationAdapter",
    "WindowsProcessAdapter",
    "WindowsSettingsAdapter",
]
