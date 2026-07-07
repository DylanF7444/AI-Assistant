# Python Skill Guide

A skill subclasses `assistant.skills.base.Skill`, supplies Pydantic input and
output models, and publishes a versioned `SkillManifest`. Implement
`execute`; optionally override `preview`, `progress`, `cancel`, `health`, and
`rollback`.

Built-in skills follow the same manifest format as installed package skills:
stable skill ID, display name, description, input schema, output schema,
declared permissions, risk level, retry flag, and rollback flag. The current
built-in feature catalog is maintained in `docs/FEATURES.md`.

Validate a packaged skill with:

```powershell
assistant-skill path\to\skill
```

Every package needs `manifest.json`. New and updated skills are disabled until
reviewed. An update that adds permissions requires renewed approval. Skills
must never invoke an unrestricted shell from model or transcript text.

Use the same feature format when documenting a new built-in or packaged skill:
status, skill ID, risk, permissions, and user-facing capability.
