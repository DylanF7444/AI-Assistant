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
- Malicious skills: new skills default disabled/no permissions. Manifests,
  duplicate IDs, API versions, schemas, timeouts, and capability expansion are
  reviewed. Packages require trusted Ed25519 signatures and execute in a
  subprocess with timeout, memory, process-count, CPU, and outbound-network
  limits.
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

## Non-guarantees

Speech alone cannot prove who is speaking. Rollback is offered only by skills
that can actually guarantee it. Python process and Job Object controls are not
a complete security sandbox against hostile native code; an OS-enforced
boundary is required before accepting arbitrary marketplace packages.
The scheduler cannot run while Windows is powered off, and this release does not
install a privileged service or request OS wake timers. Missed occurrences are
reconciled conservatively when the per-user desktop process next starts.
