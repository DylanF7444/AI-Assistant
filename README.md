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

The core and test suite do not need network access. Desktop, audio, STT, local
LLM, and TTS providers are optional and loaded only when configured.

## Safety model

Transcripts and model output are treated as untrusted input. Only registered
skills can execute. Every call is schema-validated, checked against scoped
permissions, and, for sensitive actions, bound to a short-lived immutable
confirmation request.

See `docs/USER_GUIDE.md` for setup and operation and `docs/FEATURES.md` for
the current implemented feature catalog. The remaining documents cover
architecture, offline guarantees, threat model, permissions, compatibility,
and plugin authoring.
