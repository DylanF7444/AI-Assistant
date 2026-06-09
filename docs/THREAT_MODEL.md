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

## Non-guarantees

Speech alone cannot prove who is speaking. Rollback is offered only by skills
that can actually guarantee it. Python process and Job Object controls are not
a complete security sandbox against hostile native code; an OS-enforced
boundary is required before accepting arbitrary marketplace packages.
