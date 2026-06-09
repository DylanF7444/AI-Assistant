from datetime import UTC, datetime, timedelta

from assistant.integrations.devices import (
    DeviceCommand,
    PairingManager,
    ReplayGuard,
    sign_command,
)


def test_pairing_replay_protection_and_revocation():
    pairing = PairingManager()
    code = pairing.begin("fingerprint")
    identity = pairing.approve(code, "Kitchen")
    command = DeviceCommand(device_id=identity.device_id, capability="light.on")
    key = b"local-shared-key"
    signature = sign_command(command, key)
    guard = ReplayGuard()
    assert guard.accept(command, signature, key)
    assert not guard.accept(command, signature, key)
    pairing.revoke(identity.device_id)


def test_expired_device_command_is_rejected():
    pairing = PairingManager()
    identity = pairing.approve(pairing.begin("fingerprint"), "Room")
    command = DeviceCommand(
        device_id=identity.device_id,
        capability="speaker.stop",
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    key = b"key"
    assert not ReplayGuard().accept(command, sign_command(command, key), key)

