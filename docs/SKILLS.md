# Python Skill Guide

A skill subclasses `assistant.skills.base.Skill`, supplies Pydantic input and
output models, and publishes a versioned `SkillManifest`. Implement
`execute`; optionally override `preview`, `progress`, `cancel`, `health`, and
`rollback`.

Validate a packaged skill with:

```powershell
assistant-skill path\to\skill
```

Every package needs `manifest.json`. New and updated skills are disabled until
reviewed. An update that adds permissions requires renewed approval. Skills
must never invoke an unrestricted shell from model or transcript text.

