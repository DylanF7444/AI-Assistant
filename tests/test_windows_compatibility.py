from assistant.skills.windows import (
    ApplicationWindowSkill,
    FileOperationSkill,
    WindowsDeviceControlSkill,
)
from assistant.skills.windows_features.applications import (
    ApplicationWindowSkill as SplitApplicationWindowSkill,
)
from assistant.skills.windows_features.devices import (
    WindowsDeviceControlSkill as SplitWindowsDeviceControlSkill,
)
from assistant.skills.windows_features.files import (
    FileOperationSkill as SplitFileOperationSkill,
)
from assistant.windows.adapters import (
    ApplicationAdapter,
    LocalFileSystemAdapter,
    WindowsApplicationAdapter,
)
from assistant.windows.application import (
    WindowsApplicationAdapter as SplitWindowsApplicationAdapter,
)
from assistant.windows.filesystem import (
    LocalFileSystemAdapter as SplitLocalFileSystemAdapter,
)
from assistant.windows.interfaces import ApplicationAdapter as SplitApplicationAdapter


def test_legacy_windows_adapter_imports_reexport_split_types():
    assert ApplicationAdapter is SplitApplicationAdapter
    assert WindowsApplicationAdapter is SplitWindowsApplicationAdapter
    assert LocalFileSystemAdapter is SplitLocalFileSystemAdapter


def test_legacy_windows_skill_imports_reexport_split_types():
    assert ApplicationWindowSkill is SplitApplicationWindowSkill
    assert FileOperationSkill is SplitFileOperationSkill
    assert WindowsDeviceControlSkill is SplitWindowsDeviceControlSkill
