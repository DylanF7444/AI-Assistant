# Calendar surface

The dashboard calendar is a provider-neutral read surface. It is deliberately
separate from any one extension so Google Calendar can populate it today and
future calendar integrations can use the same layout, permission, and UI
contracts.

## Composition

`assistant.calendar.contracts` defines the stable boundary:

- `CalendarLayout` stores view, selected calendar IDs, display preferences,
  and bounded fetch limits in the versioned `SettingsStore` key
  `calendar.layout.v1`.
- `CalendarQuery` carries timezone-aware, finite start/end bounds.
- `CalendarEvent` is the provider-safe display model for timed and all-day
  events.
- `CalendarSnapshot` carries events plus explicit ready, empty, stale,
  permission, connection, offline, and error states.

`CalendarProviderRegistry` is the contribution point for future extensions.
Each provider supplies a definition, status check, required capabilities, and
an async bounded collector. `CalendarService` validates the query, evaluates
the separate dashboard permission principal, enforces the global offline lock,
limits concurrent remote refreshes, and retains only an in-memory last-good
snapshot for stale/offline display.

`CalendarLayoutWidget` is a reusable Qt surface with month, week, and agenda
layouts. It renders only `CalendarSnapshot` data, masks event titles when the
global privacy setting is enabled, and exposes refresh, access, extension, and
layout-change signals. It does not know how OAuth or a provider API works.

## Google Calendar behavior

The Google provider uses the existing `integration.google_calendar` adapter and
the shared Google connection service. It reads bounded event windows through
the existing fixed-origin HTTP client, maps timed/all-day events, handles
pagination, and filters cancelled or declined events according to the layout
query. The dashboard's `Sync` action is a read-through refresh; event bodies
are not written to dashboard history, audit details, or persistent settings.

The existing Google Calendar extension remains the owner of the stable skill
ID, OAuth dialog, account authorization, and CRUD operations. Dashboard access
is a separate principal (`dashboard.google_calendar`) with its own scoped
`integration.read` and `network` grants. This keeps disabling or uninstalling
the extension fail-closed and avoids silently reusing a skill permission for a
new UI feature. The Hub's shared Google account and current full Calendar API
scope remain unchanged because the extension still supports writes; the
dashboard itself performs read-only operations.

Installing, enabling, connecting, reconnecting, or uninstalling the Google
Calendar extension causes the dashboard to re-evaluate its provider state. A
disconnected or unauthorized account shows guidance instead of stale event
details. The privacy setting masks event titles and locations while preserving
the time/block layout.

## Adding a future provider

An application-owned provider can register a `CalendarProvider` with the
registry during bootstrap:

1. Add a provider definition with a stable provider ID and refresh floor.
   Set its optional extension ID when the provider is backed by an Extension
   Hub entry so the surface can offer provider-specific setup guidance.
2. Implement `validate_query`, `status`, `required_capabilities`,
   `permission_subject_for`, and `collect`.
3. Convert remote data into `CalendarEvent` values and omit malformed items.
4. Reuse the host connection and bounded HTTP helpers; keep credentials out of
   provider arguments, snapshots, logs, and audit details.
5. Register the provider before constructing `CalendarService`.

No dashboard widget changes are required. Provider-specific calendar lists,
colors, push notifications, or incremental sync cursors can be added behind
the provider boundary later without changing the layout contract.
