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

## In-app media

`media.present` requests `filesystem.read` for one exact local source or one
approved filename-search root. An explicit HTTPS media source instead requests
`network` for the sanitized host/path target and is blocked before DNS or
download while offline lock is enabled. Downloaded cache files are
application-owned and are never treated as permission to read another local
path.

`media.viewer_control` needs no capability grant because it can target only the
currently active in-memory presentation ID. It cannot send global media keys or
control another application; that remains the separate `windows.media` skill
and `media.control` capability. Both in-app media skills are foreground-only
and cannot be scheduled for unattended execution.

When a command needs permission, the Assistant tab shows the capability,
reason, and exact target and offers to grant it directly. Normally you do not
need to type a capability in Settings. Use the Permissions tab only to review,
revoke, deny, or preconfigure grants.

## Background authorization

An interactive permission grant never authorizes a future unattended action.
Likewise, a schedule-specific background authorization cannot be used by an
interactive command. A background authorization is stored separately and binds
one schedule ID and revision to its exact frozen action digest, policy digest,
skill IDs, capabilities, resolved targets, risk ceiling, expiry, user, and local
machine identity.

Creating a fixed-action or frozen-macro schedule requires an immediate
interactive confirmation even when the future action is low risk. At every
occurrence the assistant rechecks the current skill version, input schema,
permissions, risk, file policy, target scope, run-rate/runtime/step budgets,
network allowance, lock state, authorization expiry, and action arguments.
Material edits create a new revision and revoke the previous authorization.

Only low-risk frozen work can run unattended. Medium, high, and critical work
waits in the Automations approval inbox. Approval creates a fresh exact preview
and single-use confirmation for that occurrence; critical work still requires
the displayed typed token. Schedules cannot invoke schedule management, macros,
or file-access-policy management in the background and cannot persist embedded
password, token, secret, API-key, or PIN fields.

Background compatibility is a separate check from permission and risk. Calls
that request interactive or device-changing capabilities such as
`input.control`, `process.window`, `process.launch`, clipboard access,
`media.control`, `settings.set`, process termination, or system/device control
cannot run unattended on the user's active desktop. New packaged skills default
to foreground-only until their execution mode is explicitly reviewed. See
`docs/BACKGROUND_EXECUTION.md` for the complete compatibility policy.
