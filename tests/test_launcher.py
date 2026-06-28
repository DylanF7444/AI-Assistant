import asyncio
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, SkillCall
from assistant.skills.builtin import LauncherSkill


class FakeApplications:
    def __init__(self):
        self.launched = None
        self.launched_with_args = None

    async def launch(self, path):
        self.launched = path

    async def launch_with_args(self, path, arguments):
        self.launched_with_args = (path, arguments)

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
@pytest.mark.parametrize(
    ("application", "executable", "expected_flag"),
    [
        ("brave", "brave.exe", "--incognito"),
        ("chrome", "chrome.exe", "--incognito"),
        ("edge", "msedge.exe", "--inprivate"),
        ("firefox", "firefox.exe", "-private-window"),
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
