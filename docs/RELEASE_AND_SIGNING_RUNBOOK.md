# Release, Signing, and Revocation Runbook

Updated August 6, 2026.

## Candidate build

1. Use a clean reviewed commit on a protected release branch.
2. Run `powershell -ExecutionPolicy Bypass -File scripts/build.ps1`.
3. Confirm lint, types, documentation, dependency audit, evaluation, regression,
   learning, UI, package build, and packaged-media verification all pass.
4. Inspect `dist/sbom.cdx.json`, `dist/release-manifest.json`, and
   `dist/SHA256SUMS`. A public release requires `git_dirty: false` and should
   regenerate the manifest with `--require-clean`.
5. Complete every external row in `ROADMAP_EXECUTION_REPORT.md` and record the
   environment/results in `ACCEPTANCE_RUN_LOG.md`.

## Key protection

- Keep Authenticode, model, update, and extension private keys outside the
  repository, build workspace, installer, logs, and CI artifacts.
- Prefer a hardware-backed or managed signing service with role separation,
  audit, MFA, and a documented recovery owner.
- CI and application configuration contain public keys/key IDs only.
- Sign immutable final bytes. Hash and manifest again after Authenticode changes
  the executable/installer.
- Never reuse a test key for public distribution.

## Authenticode release

1. Verify the unsigned candidate and its source-tree hash.
2. Sign `LocalAssistant.exe` and the installer using the approved timestamping
   service and protected publisher key.
3. Use Windows signature verification on both files and record publisher,
   certificate thumbprint, digest algorithm, timestamp, and result.
4. Regenerate `release-manifest.json` and `SHA256SUMS` from the signed files.
5. Publish the SBOM, manifest, checksums, release notes, supported upgrade path,
   and known external/provider requirements with the artifacts.

No certificate is present in this workspace. An unsigned candidate must never
be relabeled as a signed public release.

## Extension signing-key incident

1. Disable catalog entries signed by the affected key and stop publishing new
   packages.
2. Add the stable key ID to `revoked_skill_key_ids` through a trusted application
   configuration or update. Do not remove only the public key: explicit
   revocation produces the correct diagnosis.
3. Restart/load installed extensions. Verification occurs on every load, so a
   revoked package stays disabled and appears as a load failure.
4. Audit installed package versions/digests and preserve forensic copies outside
   the active skills directory when incident policy permits.
5. Generate a new offline private key and provision only its public key under a
   new key ID. Rebuild/review/re-sign packages; do not bless old bytes solely by
   replacing their signature.
6. Publish revocation scope, affected versions, safe replacement versions, and
   user cleanup instructions.

Signatures establish publisher identity and integrity. They do not make code
safe. Downloaded Python/native packages remain blocked by default until a real
OS-enforced hostile-code sandbox is selected and accepted.

## Application/update/model key incident

Pause distribution, remove the compromised trust root through a signed trusted
update or manual recovery channel, rotate to a new key ID, reissue only reviewed
artifacts, and publish exact compromised hashes. If no trusted update path
remains, require manual reinstall from an independently authenticated channel;
never ask the compromised build to trust an unsigned replacement.
