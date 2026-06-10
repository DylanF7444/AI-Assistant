from pathlib import Path

from assistant.contracts import PermissionContext, PermissionScope, SkillCall
from assistant.policy.permissions import GrantType, PermissionManager
from assistant.storage.database import AuditStore, Database, SettingsStore


def make_file_call(root: Path) -> SkillCall:
    return SkillCall(
        skill_id="windows.file_search",
        arguments={"root": str(root), "query": "report"},
        permission_context=PermissionContext(session_id="learning-session"),
    )


def test_filesystem_permission_allows_children_but_not_sibling_folders(
    database: Database,
    audit: AuditStore,
    tmp_path: Path,
):
    approved = tmp_path / "approved"
    child = approved / "documents"
    sibling = tmp_path / "private"
    child.mkdir(parents=True)
    sibling.mkdir()
    manager = PermissionManager(database, audit)
    manager.grant(
        user_id="local-user",
        skill_id="windows.file_search",
        capability="filesystem.read",
        grant_type=GrantType.ALWAYS,
        targets=(str(approved),),
    )

    child_scope = (
        PermissionScope(
            capability="filesystem.read",
            targets=(str(child),),
            reason="Search this folder.",
        ),
    )
    sibling_scope = (
        PermissionScope(
            capability="filesystem.read",
            targets=(str(sibling),),
            reason="Search this folder.",
        ),
    )

    assert manager.authorize(make_file_call(child), child_scope).allowed is True
    assert manager.authorize(make_file_call(sibling), sibling_scope).allowed is False


def test_settings_round_trip_structured_json(database: Database):
    settings = SettingsStore(database)

    settings.set("appearance", {"theme": "dark", "font_size": 14})

    assert settings.get("appearance") == {"theme": "dark", "font_size": 14}


def test_audit_store_redacts_secrets_at_the_storage_boundary(
    audit: AuditStore,
):
    audit.record(
        correlation_id="learning",
        event_type="example",
        details={"token": "secret-value", "nested": {"password": "hidden"}},
    )

    details = audit.query()[0]["details"]
    assert details["token"] == "[REDACTED]"
    assert details["nested"]["password"] == "[REDACTED]"
