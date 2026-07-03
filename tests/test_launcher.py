import asyncio
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, SkillCall
from assistant.skills.builtin import LauncherSkill, RegisteredApplicationsSkill


class FakeApplications:
    def __init__(self):
        self.launched = None
        self.launched_with_args = None
        self.opened_url = None

    async def launch(self, path):
        self.launched = path

    async def launch_with_args(self, path, arguments):
        self.launched_with_args = (path, arguments)

    async def open_url(self, url):
        self.opened_url = url

    async def focus(self, name):
        return True

    async def minimize(self, name):
        return True

    async def maximize(self, name):
        return True

    async def close(self, name):
        return True


def call(arguments):
    return SkillCall(
        skill_id="windows.launcher",
        arguments=arguments,
        permission_context=PermissionContext(session_id="test"),
    )


def registered_call(arguments=None):
    return SkillCall(
        skill_id="windows.registered_applications",
        arguments=arguments or {},
        permission_context=PermissionContext(session_id="test"),
    )


@pytest.mark.asyncio
async def test_launcher_uses_registered_path_only(tmp_path: Path):
    approved = tmp_path / "approved.exe"
    other = tmp_path / "other.exe"
    approved.write_bytes(b"")
    other.write_bytes(b"")
    adapter = FakeApplications()
    skill = LauncherSkill(
        adapter, resolver=lambda name: str(approved) if name == "approved" else None
    )
    await skill.execute(call({"application": "approved"}), asyncio.Event())
    assert adapter.launched == str(approved)
    with pytest.raises(ValueError, match="does not match"):
        await skill.execute(
            call(
                {
                    "application": "approved",
                    "registered_path": str(other),
                }
            ),
            asyncio.Event(),
        )
    with pytest.raises(ValueError, match="not registered"):
        skill.required_permissions(call({"application": "unknown"}))


@pytest.mark.asyncio
async def test_registered_applications_skill_lists_assistant_registrations(tmp_path: Path):
    brave = tmp_path / "brave.exe"
    notepad = tmp_path / "notepad.exe"
    skill = RegisteredApplicationsSkill(
        lambda: {"brave": str(brave), "notepad": str(notepad)}
    )

    result = await skill.execute(registered_call(), asyncio.Event())

    assert result.summary == "2 applications are registered."
    assert "brave" in result.display_text
    assert str(brave) in result.display_text
    assert result.data == {
        "applications": [
            {"name": "brave", "path": str(brave)},
            {"name": "notepad", "path": str(notepad)},
        ]
    }


@pytest.mark.asyncio
async def test_registered_applications_skill_handles_empty_list():
    skill = RegisteredApplicationsSkill(lambda: {})

    result = await skill.execute(registered_call(), asyncio.Event())

    assert result.summary == "No applications are registered yet."
    assert "Applications tab" in result.display_text
    assert result.data == {"applications": []}


@pytest.mark.asyncio
async def test_launcher_opens_registered_path_with_https_url(tmp_path: Path):
    approved = tmp_path / "approved.exe"
    approved.write_bytes(b"")
    adapter = FakeApplications()
    skill = LauncherSkill(
        adapter, resolver=lambda name: str(approved) if name == "approved" else None
    )
    result = await skill.execute(
        call({"application": "approved", "url": "https://example.com/search?q=test"}),
        asyncio.Event(),
    )

    assert adapter.launched is None
    assert adapter.launched_with_args == (
        str(approved),
        ("https://example.com/search?q=test",),
    )
    assert "https://example.com/search?q=test" in result.summary
    with pytest.raises(ValueError, match="public HTTPS"):
        skill.validate({"application": "approved", "url": "file:///C:/secret.txt"})


@pytest.mark.asyncio
async def test_launcher_opens_fallback_url_in_default_browser_when_unregistered():
    adapter = FakeApplications()
    skill = LauncherSkill(adapter, resolver=lambda name: None)

    result = await skill.execute(
        call({"application": "youtube", "fallback_url": "https://www.youtube.com"}),
        asyncio.Event(),
    )

    assert adapter.launched is None
    assert adapter.launched_with_args is None
    assert adapter.opened_url == "https://www.youtube.com"
    assert "https://www.youtube.com" in result.summary
    permission = skill.required_permissions(
        call({"application": "youtube", "fallback_url": "https://www.youtube.com"})
    )[0]
    assert permission.capability == "network"
    assert permission.targets == ("https://www.youtube.com",)


@pytest.mark.asyncio
async def test_launcher_prefers_registered_application_over_website_fallback(
    tmp_path: Path,
):
    approved = tmp_path / "youtube.exe"
    approved.write_bytes(b"")
    adapter = FakeApplications()
    skill = LauncherSkill(adapter, resolver=lambda name: str(approved))

    await skill.execute(
        call({"application": "youtube", "fallback_url": "https://www.youtube.com"}),
        asyncio.Event(),
    )

    assert adapter.launched == str(approved)
    assert adapter.opened_url is None
    assert skill.required_permissions(
        call({"application": "youtube", "fallback_url": "https://www.youtube.com"})
    )[0].capability == "process.launch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("application", "executable", "expected_flag"),
    [
        ("brave", "brave.exe", "--incognito"),
        ("chrome", "chrome.exe", "--incognito"),
        ("chromium", "chromium.exe", "--incognito"),
        ("edge", "msedge.exe", "--inprivate"),
        ("firefox", "firefox.exe", "-private-window"),
        ("firefox nightly", "firefox.exe", "-private-window"),
        ("librewolf", "librewolf.exe", "-private-window"),
        ("opera", "opera.exe", "--private"),
        ("opera gx", "launcher.exe", "--private"),
        ("vivaldi", "vivaldi.exe", "--incognito"),
        ("waterfox", "waterfox.exe", "-private-window"),
    ],
)
async def test_launcher_opens_private_browser_window(
    tmp_path: Path, application: str, executable: str, expected_flag: str
):
    approved = tmp_path / executable
    approved.write_bytes(b"")
    adapter = FakeApplications()
    skill = LauncherSkill(adapter, resolver=lambda name: str(approved))

    result = await skill.execute(
        call({"application": application, "private": True}),
        asyncio.Event(),
    )

    assert adapter.launched is None
    assert adapter.launched_with_args == (str(approved), (expected_flag,))
    assert result.data == {"application": application, "private": True}
    assert "private window" in result.summary


@pytest.mark.asyncio
async def test_launcher_opens_private_browser_with_https_url(tmp_path: Path):
    approved = tmp_path / "brave.exe"
    approved.write_bytes(b"")
    adapter = FakeApplications()
    skill = LauncherSkill(adapter, resolver=lambda name: str(approved))

    await skill.execute(
        call(
            {
                "application": "brave",
                "private": True,
                "url": "https://example.com/",
            }
        ),
        asyncio.Event(),
    )

    assert adapter.launched_with_args == (
        str(approved),
        ("--incognito", "https://example.com/"),
    )
