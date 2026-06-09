import asyncio
from pathlib import Path

import pytest

from assistant.contracts import PermissionContext, SkillCall
from assistant.skills.builtin import LauncherSkill


class FakeApplications:
    def __init__(self):
        self.launched = None

    async def launch(self, path):
        self.launched = path

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
