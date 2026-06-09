from assistant.contracts import PermissionContext, RiskLevel, SkillCall
from assistant.policy.confirmations import ConfirmationManager


def make_call(arguments=None):
    return SkillCall(
        skill_id="test.action",
        arguments=arguments or {"target": "a"},
        permission_context=PermissionContext(session_id="s"),
    )


def test_confirmation_is_bound_to_immutable_action():
    manager = ConfirmationManager()
    call = make_call()
    request = manager.create(
        call, exact_action="Delete a", targets=("a",), risk=RiskLevel.HIGH
    )
    changed = call.model_copy(update={"arguments": {"target": "b"}})
    assert not manager.approve(request.confirmation_id, changed)


def test_critical_confirmation_requires_typed_token():
    manager = ConfirmationManager()
    call = make_call()
    request = manager.create(
        call, exact_action="Critical", targets=("a",), risk=RiskLevel.CRITICAL
    )
    assert not manager.approve(request.confirmation_id, call)
    request = manager.create(
        call, exact_action="Critical", targets=("a",), risk=RiskLevel.CRITICAL
    )
    assert manager.approve(
        request.confirmation_id,
        call,
        typed_token=str(request.confirmation_id)[-6:],
    )

