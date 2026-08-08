# Threat Model

## Protected assets

Local files, applications, clipboard content, messages, devices, secrets,
transcripts, permissions, and the integrity of audit records.

## Major threats and controls

- Voice spoofing: voice is input, not identity. Sensitive actions still require
  scoped permission and action-bound confirmation; critical actions require a
  typed token or future Windows authentication.
- Prompt injection: model text cannot execute. Only retrieved registered skills
  can be selected, arguments must satisfy JSON Schema, and policy is enforced
  outside the model.
- Playbook abuse: only enabled local Markdown playbooks receive user-guidance
  precedence. Their frontmatter is schema-validated and size-bounded, listed
  skill IDs are intersected with enabled registered skills, and instructions
  cannot grant authority or weaken policy. Playbooks are not interpreted by the
  unattended scheduler.
- Malicious skills: new skills default disabled/no permissions. Manifests,
  duplicate IDs, API versions, schemas, timeouts, capability expansion, archive
  bounds, path traversal, symlinks, signer metadata, and revoked signer IDs are
  reviewed. Core and first-party managed code may run in process. Downloaded
  Python packages are blocked by default; a reviewed-worker opt-in is available
  only for controlled development because process/Job limits are not an OS
  security sandbox. Native downloaded extensions remain blocked.
- Confused confirmation: confirmation stores the exact call digest,
  correlation ID, targets, risk, and expiration. It is consumed once.
- Filesystem escape: permissions compare resolved targets against approved
  roots. Deletion uses the Recycle Bin and remains high risk.
- Network exfiltration: offline mode blocks assistant and worker outbound
  connections while allowing loopback runtimes. Network integrations require
  explicit configuration and permission. OS firewall/AppContainer isolation is
  still required before accepting hostile native third-party code.
- Compromised device: pairing requires local approval; commands expire, use
  nonces and authenticated signatures, and identities can be revoked.
- Unattended execution: the scheduler can execute only registered frozen calls
  through the normal engine. A separate background authorization binds the
  schedule revision, action and policy digests, skills, capabilities, exact
  targets, risk, expiry, and machine identity. Interactive grants are not
  accepted as background authority.
- Foreground interference: schedule authoring and execution independently reject
  input, window, launch/termination, clipboard, media, and device-changing
  capabilities. An unlocked session does not make those actions background-safe.
- Headless browsing: every read uses a fresh non-persistent context; only public
  HTTPS requests are allowed, private/local targets, mutating HTTP methods,
  downloads, service workers, and WebSockets are blocked, output and extracted
  result cards are bounded, and the user's normal browser profile is not reused.
- Remote media SSRF and decoder abuse: only an explicit HTTPS URL without
  credentials is accepted. Resolved and redirected targets must be public;
  response bytes and time are bounded; MIME category and bounded signature
  probing must agree; Qt receives only a verified local cache path; file
  fingerprint and image dimensions are rechecked before decoding. Unsupported
  codecs fail inside the viewer without falling back to an external process.
- Media privacy: URL query strings and credentials are removed from provenance,
  audit, and terminal diagnostics. Decoded frames, thumbnails, file content,
  active paths, and playback positions are not persisted. Cache age and bytes
  are bounded and the Privacy tab can clear downloads independently.
- Speaker-to-microphone feedback: source-aware TTS and viewer focus keeps
  capture suppressed until all output stops. Explicit push-to-talk pauses viewer
  media; passive activation stays suppressed during speaker playback. Emergency
  Stop and shutdown release playback resources.
- Stored-action tampering and prompt injection: schedule payloads are typed data,
  never prompts interpreted at fire time. Action/policy digest mismatches,
  changed arguments, schema/permission/risk/version drift, disabled skills, and
  file-policy changes block execution and can revoke authority.
- Crash replay: every occurrence and transition is durable. Leases are renewed
  during execution; a crash after execution begins becomes
  `unknown_after_crash`. Unsafe failures and unknown outcomes are never retried;
  only skills explicitly marked safe may use the bounded durable retry policy.
- Resource abuse: recurrence has a one-minute minimum, catch-up and concurrent
  queues are capped, schedules have hourly/daily/step/runtime limits, repeated
  failures disable the schedule, and emergency stop pauses new claims before
  canceling active jobs.
- Restored data: background authorizations are tied to a non-secret digest of
  the local machine identity. A database restored elsewhere moves affected
  schedules to pending review.
- Document and image disclosure: content extraction requires the separate
  exact-target `filesystem.content.read` grant. Folder indexing requires a
  second retention grant, is bounded, detects modified/deleted files, and can be
  cleared independently. Citations use opaque references and sanitized
  locators. Visual understanding accepts one explicit file and has no screen,
  camera, clipboard, or input path.
- Consequential provider writes: each write operation uses a narrow schema,
  exact target, fixed provider origin, operation-specific permission, preview,
  confirmation, and non-retryable execution. Transfers refuse overwrite;
  Outlook sends only an existing draft; unsupported bulk/destructive operations
  are absent from the schema.
- Event-monitor abuse: observation types have separate exact-target scopes and
  minimum polling intervals. Sources retain bounded filename/process/device/
  calendar metadata or hashes, not document/web bodies or process command
  lines. Public-HTTPS checks reject credentials and private targets, discard
  query/fragment before persistence, obey offline lock, and cannot execute an
  action outside the frozen schedule authorization.

## Non-guarantees

Speech alone cannot prove who is speaking. Rollback is offered only by skills
that can actually guarantee it. Python process and Job Object controls are not
a complete security sandbox against hostile native code; an OS-enforced
boundary is required before accepting arbitrary marketplace packages.
The scheduler cannot run while Windows is powered off, and this release does not
install a privileged service or request OS wake timers. Missed occurrences are
reconciled conservatively when the per-user desktop process next starts.
