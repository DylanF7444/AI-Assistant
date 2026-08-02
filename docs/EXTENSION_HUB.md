# Extension Hub and Platform Contract

The desktop **Extension Hub** browses a configured HTTPS catalog and installs
compatible signed skill packages. Configure the catalog URL in **Settings**,
disable the global offline lock, restart the application, and open the hub.

Discovery does not grant authority. The catalog can describe a package, but it
cannot add a trusted signing key, enable a skill, or grant permissions. A newly
installed skill is disabled and receives no permission grants. Review and
enable it separately in **Skills**. An installed catalog entry can be removed
from its Hub detail page with **Uninstall Extension**; removal disables the
skill, clears its runtime registration, deletes its local package, and leaves
the entry available for a future reinstall.

## Catalog API

The configured URL is an HTTPS `GET` endpoint returning at most 2 MiB of UTF-8
JSON. Catalog schema `1.0` has this form:

```json
{
  "schema_version": "1.0",
  "extensions": [
    {
      "extension_id": "publisher.example",
      "extension_type": "skill",
      "version": "1.0.0",
      "api_version": "1.0",
      "display_name": "Example Extension",
      "description": "Perform an example operation.",
      "publisher": "Example Publisher",
      "category": "Productivity",
      "package_url": "https://cdn.example.com/publisher.example-1.0.0.zip",
      "package_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "package_format": "zip",
      "permissions": [
        {
          "capability": "network",
          "targets": [],
          "reason": "Reach the approved service."
        }
      ],
      "risk": "low",
      "featured": false
    }
  ]
}
```

This build displays every valid entry but installs only `skill` extensions
targeting core API `1.0`. Unsupported types and versions remain visible and
have a disabled install control.

## Package requirements

`package_url` must be public HTTPS. Downloads are bounded by
`extension_package_max_bytes`, which defaults to 25 MiB. The package is a ZIP
archive containing one skill package at its root or in one top-level folder:

```text
manifest.json
signature.json
plugin.py
```

Installation verifies, in order:

1. Download size and catalog SHA-256 digest.
2. Archive file count, expanded size, paths, and absence of symbolic links.
3. The trusted Ed25519 package signature.
4. Core API compatibility and manifest schema.
5. Exact agreement between catalog and manifest identity, version, display
   metadata, permissions, and risk.
6. Successful loading into the isolated skill worker boundary.

Any failure removes the staged or partially installed package and records a
local audit event. Package signer keys come from the build's
`trusted_skill_keys` configuration. Keys supplied by catalog content are never
trusted. Publisher-key additions and revocations therefore require a trusted
application configuration or build update.

The platform does not receive permission grants, execution history, local
files, or credentials from the hub. Installed skill execution continues through
the normal local schema, permission, confirmation, audit, timeout, and offline
controls described in `docs/SKILLS.md` and `docs/PERMISSIONS.md`.

## First-party managed extensions

The Hub can also list optional first-party integrations shipped with the
application. These entries remain installable when no external catalog is
configured. Installing one persists its reviewed manifest and makes the shipped
host implementation available disabled; it does not execute downloaded package
code. Community packages continue to use the isolated worker boundary.

First-party account connections are configured from the extension's Hub detail
page. OAuth tokens remain in the host Windows-encrypted secret vault and are
never passed to either first-party extension arguments or community package
workers. Google Calendar is the first managed extension using this path.
