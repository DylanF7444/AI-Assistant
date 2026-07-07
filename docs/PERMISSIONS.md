# Permissions

Permissions are denied by default and granted to one stable skill ID.

- `filesystem.read`: list, inspect, or search approved folders.
- `filesystem.write`: create, copy, move, rename, or recycle approved files.
- `notification.create`: show a visible local desktop notification.
- `process.launch`: launch only registered application paths.
- `process.terminate`: stop an exact process after confirmation.
- `process.window`: focus or change an approved visible application window.
- `clipboard.read` and `clipboard.write`: separate clipboard capabilities.
- `media.control`: send local playback commands.
- `input.control`: visible keyboard/mouse automation in approved applications.
- `network`: make outbound connections while the global offline lock is off.
- `messaging`: send to approved contacts.
- `settings.get`: read an approved assistant or Windows setting.
- `settings.set`: change an approved assistant or Windows setting.
- `device.control`: operate an approved paired device or enable/disable one
  approved local connected device by instance ID.
- `device.read`: read local or paired-device status such as battery, storage,
  performance, and connected devices.
- `system.apps`: search installed local applications.
- `system.display`: change local display settings such as brightness.
- `system.network`: enable, disable, or toggle local radios such as Wi-Fi and
  Bluetooth.
- `system.power`: lock, restart, or shut down the local device.

Grant modes are one-time, current session, always, and deny. Targets can narrow
grants to folders, applications, contacts, or devices. Medium, high, and
critical actions additionally require immediate confirmation.

The file access policy is separate from user grants. A blacklist entry blocks
matching filesystem access even when a permission grant exists. A whitelist
limits matching filesystem access to configured paths. Policy blocks are
reported as policy denials, not as permissions the user can grant around.

When a command needs permission, the Assistant tab shows the capability,
reason, and exact target and offers to grant it directly. Normally you do not
need to type a capability in Settings. Use the Permissions tab only to review,
revoke, deny, or preconfigure grants.
