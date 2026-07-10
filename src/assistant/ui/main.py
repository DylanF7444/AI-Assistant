from __future__ import annotations

from assistant.bootstrap import Container


def run_desktop(container: Container) -> int:
    """Load the optional Qt desktop implementation only when it is requested."""
    from assistant.ui.desktop.app import run_desktop as run_desktop_application

    return run_desktop_application(container)
