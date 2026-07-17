# Background Execution

Background execution runs on the user's machine without controlling or
rearranging the interactive desktop. It does not use a second desktop and does
not treat an unlocked session as permission to steal focus.

## Compatibility policy

Every skill manifest declares one of three modes:

- `safe`: core code explicitly designed for hidden execution, such as the
  isolated headless reader.
- `capability_gated`: eligibility is decided from the exact call's resolved
  permissions.
- `foreground_only`: the skill cannot be scheduled as unattended work.

New externally packaged skills default to `foreground_only`. Built-in skills
using `capability_gated` are checked when a schedule is authored and again just
before every occurrence. The ordinary permission, target, file-policy, risk,
confirmation, timeout, rate, cancellation, audit, and schedule-authorization
checks still apply after compatibility is established.

Background-eligible capabilities include:

- local calculation, database, task, note, memory, diagnostic, and timer work
  that declares no protected capability;
- `filesystem.read` and non-UI `filesystem.write` operations;
- `network`, including public web APIs and the isolated headless reader;
- `notification.create`;
- read-only `device.read`, `settings.get`, `system.apps`, and process-list
  operations.

The following capabilities are foreground-only because they can steal focus,
use volatile interactive state, or visibly alter the device the user is using:

- `input.control`, `process.window`, `process.launch`, `process.terminate`, and
  `window.control`;
- `clipboard.read` and `clipboard.write`;
- `media.control`, `settings.set`, `system.display`, `system.network`,
  `system.power`, and `device.control`.

Individual skills can be more restrictive. For example, copying a file is
background-eligible, while opening that file requests `process.launch` and is
therefore foreground-only. Higher-risk background-eligible actions still wait
for occurrence-specific approval.

## Headless browser

Install the optional browser integration with:

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

The `web.headless_read` skill renders one public HTTPS page, extracts bounded
visible text and links, and then closes the browser. Each call creates a fresh
non-persistent context with downloads disabled, service workers blocked, local
and private network targets rejected, WebSockets blocked, and image/media/font
requests omitted.
It never connects to a visible user browser or reuses that browser's profile,
cookies, extensions, or saved credentials.

Examples:

- `read https://example.com in the background`
- `render https://example.com with a headless browser`
- `headlessly fetch https://example.com`

The current headless skill is deliberately read-only. Authenticated sessions,
form submission, file download, purchases, messages, and other website-side
mutations require separate typed skills and permission/risk review.

## Activity trace

The Assistant page shows a bounded user-facing trace with wall-clock times for:

1. understanding the request;
2. selecting a route or skill;
3. checking permissions and confirmation requirements;
4. starting an action;
5. reporting its result.

This is an operational decision trace, not private model chain-of-thought. It
does not display raw skill arguments or retrieved private content. Background
entries are labeled separately and update a `BACKGROUND` active/idle indicator
without changing the foreground command status or disabling its controls. The
existing audit log remains the durable security record.
