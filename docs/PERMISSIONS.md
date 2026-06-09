# Permissions

Permissions are denied by default and granted to one stable skill ID.

- `filesystem.read`: list, inspect, or search approved folders.
- `filesystem.write`: create, copy, move, rename, or recycle approved files.
- `process.launch`: launch only registered application paths.
- `process.terminate`: stop an exact process after confirmation.
- `clipboard.read` and `clipboard.write`: separate clipboard capabilities.
- `media.control`: send local playback commands.
- `input.control`: visible keyboard/mouse automation in approved applications.
- `network`: make outbound connections while the global offline lock is off.
- `messaging`: send to approved contacts.
- `device.control`: operate an approved paired device.

Grant modes are one-time, current session, always, and deny. Targets can narrow
grants to folders, applications, contacts, or devices. Medium, high, and
critical actions additionally require immediate confirmation.

When a command needs permission, the Assistant tab shows the capability,
reason, and exact target and offers to grant it directly. Normally you do not
need to type a capability in Settings. Use the Permissions tab only to review,
revoke, deny, or preconfigure grants.
