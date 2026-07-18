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
LLM, and TTS providers are optional and loaded only when configured.

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

## Safety model

Transcripts and model output are treated as untrusted input. Only registered
skills can execute. Every call is schema-validated, checked against scoped
permissions, and, for sensitive actions, bound to a short-lived immutable
confirmation request.

See `docs/USER_GUIDE.md` for setup and operation and `docs/FEATURES.md` for
the current implemented feature catalog. The remaining documents cover
architecture, offline guarantees, threat model, permissions, compatibility,
and plugin authoring.
