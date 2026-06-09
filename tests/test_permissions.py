from pathlib import Path
from uuid import uuid4

from assistant.contracts import PermissionContext, PermissionScope, SkillCall
from assistant.policy.permissions import GrantType, PermissionManager
from assistant.storage.database import AuditStore, Database


def build(tmp_path):
    database = Database(f"sqlite:///{tmp_path / 'data.db'}")
    database.migrate()
    audit = AuditStore(database)
    return PermissionManager(database, audit), audit


def call(target: str):
    return SkillCall(
        skill_id="test.files",
        arguments={"path": target},
        permission_context=PermissionContext(session_id="s"),
    )


def test_permissions_default_deny_and_folder_scope(tmp_path: Path):
    manager, _ = build(tmp_path)
    root = tmp_path / "approved"
    root.mkdir()
    inside = root / "file.txt"
    outside = tmp_path / "outside.txt"
    required = (
        PermissionScope(
            capability="filesystem.read",
            targets=(str(inside),),
            reason="test",
        ),
    )
    assert not manager.authorize(call(str(inside)), required).allowed
    manager.grant(
        user_id="local-user",
        skill_id="test.files",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(root),),
    )
    assert manager.authorize(call(str(inside)), required).allowed
    outside_scope = (
        required[0].model_copy(update={"targets": (str(outside),)}),
    )
    assert not manager.authorize(call(str(outside)), outside_scope).allowed


def test_one_time_permission_is_consumed(tmp_path: Path):
    manager, audit = build(tmp_path)
    required = (
        PermissionScope(capability="media.control", reason="test"),
    )
    skill_call = SkillCall(
        skill_id="test.media",
        permission_context=PermissionContext(session_id="s"),
    )
    manager.grant(
        user_id="local-user",
        skill_id="test.media",
        capability="media.control",
        grant_type=GrantType.ONCE,
    )
    assert manager.authorize(skill_call, required).allowed
    assert not manager.authorize(skill_call, required).allowed
    assert audit.query(event_type="permission_changed")


def test_permission_without_targets_allows_any_target(tmp_path: Path):
    manager, _ = build(tmp_path)
    required = (
        PermissionScope(
            capability="filesystem.read",
            targets=(str(tmp_path / "anywhere" / "file.txt"),),
            reason="test",
        ),
    )
    manager.grant(
        user_id="local-user",
        skill_id="test.files",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
    )
    assert manager.authorize(call(required[0].targets[0]), required).allowed
