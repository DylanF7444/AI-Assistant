# Python Skill Guide

A skill subclasses `assistant.skills.base.Skill`, supplies Pydantic input and
output models, and publishes a versioned `SkillManifest`. Implement
`execute`; optionally override `preview`, `progress`, `cancel`, `health`, and
`rollback`.

Built-in skills follow the same manifest format as installed package skills:
stable skill ID, display name, description, input schema, output schema,
declared permissions, risk level, retry flag, and rollback flag. The current
built-in feature catalog is maintained in `docs/FEATURES.md`.

Manifests also declare `background_mode`: `safe`, `capability_gated`, or
`foreground_only`. Packaged skills default to `foreground_only`. Selecting
another mode is a security claim that must be reviewed together with every
resolved permission; capability-gated execution still rejects the interactive
and device-changing capabilities listed in `docs/BACKGROUND_EXECUTION.md`.

Every package needs `manifest.json`. Validate release packages with a
`SkillRegistry` configured with the publisher's trusted public key; the exact
signing and validation workflow is in `docs/ADDING_EXTENSIONS.md`. New and
updated skills are disabled until reviewed. An update that adds permissions
requires renewed approval. Skills must never invoke an unrestricted shell from
model or transcript text.

The desktop Extension Hub can discover compatible packages from a configured
HTTPS catalog. Catalog presence is not trust: installation also requires a
matching SHA-256 digest, a signature from a locally trusted publisher key, safe
archive paths, API compatibility, and exact catalog/manifest agreement. See
`docs/EXTENSION_HUB.md` for the platform contract and
`docs/ADDING_EXTENSIONS.md` for the contributor setup checklist.

Use the same feature format when documenting a new built-in or packaged skill:
status, skill ID, risk, permissions, and user-facing capability.

## User playbooks are not executable skills

The Playbooks feature accepts UTF-8 Markdown files containing YAML frontmatter
and natural-language workflow instructions. A playbook may name registered
skill IDs and add guidance for how each skill should be used. Those IDs improve
retrieval and grounding; they do not grant permission or make an unavailable
skill callable.

```markdown
---
id: prepare-release
name: Prepare a release
description: Check whether a project is ready to release.
version: 1
enabled: true
triggers:
  - prepare a release
skills:
  - id: core.datetime
    instructions: Use only if the checklist needs the current date.
---

# Workflow

1. Identify the project and success criteria.
2. Run read-only checks first.
3. Summarize failures before proposing changes.
4. Ask before any sensitive action.
```

Playbooks live under `<data_dir>/playbooks`, can be managed in the Playbooks
tab, and are retrieved only for planner-handled requests. Actual actions always
remain typed registered skill calls processed by the normal execution engine.

The `core.playbook` management skill backs deterministic user commands for
save, list, show/recall, enable, disable, and delete. For example:

```text
save playbook Release Check using skills core.datetime, core.tasks: First inspect the notes. Then create a checklist.
use playbook Release Check to prepare version 2.0
```

Explicit use injects the exact enabled playbook into planner context. It does
not execute the listed IDs directly, and deletion remains confirmation-gated.
Playbooks cannot list `core.playbook` as skill guidance, preventing recursive
playbook-management workflows.
