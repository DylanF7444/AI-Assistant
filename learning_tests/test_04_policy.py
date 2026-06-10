from assistant.contracts import (
    PermissionContext,
    PermissionScope,
    RiskLevel,
    SkillCall,
)
from assistant.policy.capabilities import available_capabilities
from assistant.policy.confirmations import ConfirmationManager


def make_call(target: str = "report.txt") -> SkillCall:
    return SkillCall(
        skill_id="example.delete",
        arguments={"target": target},
        permission_context=PermissionContext(session_id="learning-session"),
    )


def test_confirmation_is_bound_to_the_exact_arguments():
    manager = ConfirmationManager()
    original = make_call("report.txt")
    request = manager.create(
        original,
        exact_action="Delete report.txt",
        targets=("report.txt",),
        risk=RiskLevel.HIGH,
    )
    changed = original.model_copy(update={"arguments": {"target": "other.txt"}})

    assert manager.approve(request.confirmation_id, changed) is False


def test_critical_confirmation_requires_the_displayed_token():
    manager = ConfirmationManager()
    call = make_call()
    request = manager.create(
        call,
        exact_action="Permanently delete report.txt",
        targets=("report.txt",),
        risk=RiskLevel.CRITICAL,
    )
    token = str(request.confirmation_id)[-6:]

    assert request.requires_typed_token is True
    assert manager.approve(request.confirmation_id, call, typed_token=token) is True


def test_declared_plugin_capabilities_are_added_to_builtin_capabilities():
    custom = PermissionScope(
        capability="calendar.write",
        reason="Create a calendar event.",
    )

    capabilities = available_capabilities((custom,))

    assert "filesystem.read" in capabilities
    assert "calendar.write" in capabilities
    assert capabilities == tuple(sorted(capabilities))
