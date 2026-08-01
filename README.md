# Local Assistant

An offline-first Windows desktop assistant with typed skill contracts, explicit
permissions, action-bound confirmations, deterministic routing, and a local
audit trail.

## Quick start

```powershell
python -m pip install -e .[test]
python -m pytest
python -m assistant.app
```

On Windows, `scripts/run.ps1` starts the app from the repository root without
requiring a manually composed `cd` command:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run.ps1
```

If you change directories explicitly, quote paths containing spaces:
`Set-Location -LiteralPath 'C:\path with spaces\Assistant'`.

Development and release checks:

```powershell
python -m pip install -e ".[test,dev,ui-test]"
python -m ruff format --check .
python -m ruff check .
python -m pytest
python -m pytest learning_tests -q
python -m pytest -m ui tests/ui -q
powershell -ExecutionPolicy Bypass -File scripts/build.ps1
```

The core and test suite do not need network access. Desktop, audio, STT, local
LLM, and TTS providers are optional and loaded only when configured. Speech can
use local Piper voice files or ElevenLabs voice/model IDs; ElevenLabs remains
disabled under the global offline lock.

Read JavaScript-rendered public pages and extract matching result cards without
opening a user-visible browser:

```powershell
python -m pip install -e ".[browser]"
```

The browser extra is optional. It uses a fresh non-persistent headless context
and never attaches to the user's normal browser profile. It can use an installed
Microsoft Edge or Google Chrome runtime. `python -m playwright install chromium`
remains available on systems without either browser. For example, “Go to
Slickdeals and give me the first deal” renders the site and returns the first
matching deal card in the assistant.

User-authored Markdown playbooks can guide multi-step planning with natural
language instructions and optional registered skill IDs. Playbooks remain
planning guidance: every resulting call still uses the normal typed skill,
permission, confirmation, and audit pipeline.

The desktop Extension Hub can browse a configured HTTPS catalog and install
compatible signed skill packages. Platform discovery never enables an extension
or grants it permissions; see `docs/EXTENSION_HUB.md` for the catalog contract.

The full desktop has two primary screens: a customizable metrics Dashboard and
the conversational Assistant. The Dashboard includes local assistant and
automation cards, permission-gated Windows device metrics, and exact,
read-only Home Assistant entity cards. External access honors the offline lock,
keeps credentials in the encrypted vault, and stores numeric trend history only
when the user enables it for a connection.

The Assistant also has a permission-gated, separate results window for local or
explicitly downloaded images, audio, video, and completed background reports.
It provides image
zoom/pan/rotate and audio/video transport, seek, volume, rate, mute, compact-card,
fullscreen, voice, and keyboard controls. Remote media is downloaded into a
bounded verified cache and is unavailable under offline lock. See
`docs/MEDIA_VIEWER.md`.

Background reminders and results are retained in a durable notification Inbox
with unread badges, a recent-item drawer, in-app banners, Windows click-through,
quiet hours, and links to the exact automation run.

## Safety model

Transcripts and model output are treated as untrusted input. Only registered
skills can execute. Every call is schema-validated, checked against scoped
permissions, and, for sensitive actions, bound to a short-lived immutable
confirmation request.

See `docs/USER_GUIDE.md` for setup and operation and `docs/FEATURES.md` for
the current implemented feature catalog. The remaining documents cover
architecture, offline guarantees, threat model, permissions, compatibility,
media presentation, and plugin authoring.
