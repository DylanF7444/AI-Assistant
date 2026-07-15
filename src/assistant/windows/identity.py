from __future__ import annotations

import hashlib
import platform
import uuid


def current_machine_identity() -> str:
    """Return a stable, non-secret digest for restore-bound authorization checks."""
    source = None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            source = str(winreg.QueryValueEx(key, "MachineGuid")[0])
    except (ImportError, OSError):
        source = f"{platform.node()}:{uuid.getnode()}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
