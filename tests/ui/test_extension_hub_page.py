from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pytestqt")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from assistant.extensions.platform import CatalogExtension, ExtensionCatalog
from assistant.ui.desktop.extension_hub import ExtensionHubPage


class _FakeHub:
    def __init__(self, extension: CatalogExtension) -> None:
        self.catalog_url = None
        self.catalog = ExtensionCatalog()
        self.extension = extension
        self.installed = False

    async def refresh(self):
        self.catalog = ExtensionCatalog(extensions=(self.extension,))
        return self.catalog.extensions

    def installation_status(self, extension_id: str) -> str:
        assert extension_id == self.extension.extension_id
        return "installed" if self.installed else "available"

    async def install(self, extension_id: str):
        assert extension_id == self.extension.extension_id
        self.installed = True
        return SimpleNamespace(
            skill_id=extension_id,
            display_name=self.extension.display_name,
        )

    def connection_service(self, _extension_id: str):
        return None


@pytest.mark.ui
def test_extension_hub_page_browses_reviews_and_installs(qtbot, monkeypatch):
    extension = CatalogExtension(
        extension_id="community.weather",
        extension_type="skill",
        version="1.0.0",
        api_version="1.0",
        display_name="Community Weather",
        description="Read the current weather from an approved service.",
        publisher="Example Publisher",
        category="Information",
        package_url="https://extensions.example.test/community.weather.zip",
        package_sha256="0" * 64,
        permissions=(
            {
                "capability": "network",
                "reason": "Read weather from the approved service.",
            },
        ),
    )
    service = _FakeHub(extension)
    page = ExtensionHubPage(service, offline=False)
    qtbot.addWidget(page)
    service.catalog_url = "https://extensions.example.test/v1/catalog.json"
    page.set_configuration(service.catalog_url, offline=False)

    qtbot.mouseClick(page.refresh_button, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: page.extension_list.count() == 1, timeout=2000)

    assert page.detail_title.text() == "Community Weather"
    assert "Example Publisher" in page.publisher_label.text()
    assert page.permission_table.item(0, 0).text() == "network"
    assert page.install_button.isEnabled()

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )
    with qtbot.waitSignal(page.installed, timeout=2000) as installed:
        qtbot.mouseClick(page.install_button, Qt.MouseButton.LeftButton)

    assert installed.args == ["community.weather"]
    qtbot.waitUntil(lambda: service.installed, timeout=2000)
    assert not page.install_button.isEnabled()
    assert "Skills tab" in page.install_status.text()


@pytest.mark.ui
def test_first_party_extension_is_visible_without_platform_catalog(qtbot):
    extension = CatalogExtension(
        extension_id="integration.google_calendar",
        version="1.0.0",
        api_version="1.0",
        display_name="Google Calendar",
        description="Read and manage Google Calendar.",
        publisher="Local Assistant",
        category="Connections",
        package_url="https://extensions.localassistant.invalid/google-calendar",
        package_sha256="0" * 64,
        source="first_party",
    )
    service = _FakeHub(extension)
    service.catalog = ExtensionCatalog(extensions=(extension,))

    page = ExtensionHubPage(service, offline=False)
    qtbot.addWidget(page)

    assert page.extension_list.count() == 1
    assert page.detail_title.text() == "Google Calendar"
    assert page.install_button.isEnabled()
    assert "first-party" in page.install_status.text().casefold()
