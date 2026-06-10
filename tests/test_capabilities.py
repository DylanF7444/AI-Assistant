from assistant.contracts import PermissionScope
from assistant.policy.capabilities import available_capabilities


def test_available_capabilities_include_built_ins_and_skill_declarations():
    capabilities = available_capabilities(
        (PermissionScope(capability="custom.export", reason="Export data"),)
    )

    assert "filesystem.read" in capabilities
    assert "custom.export" in capabilities
    assert capabilities == tuple(sorted(capabilities))
