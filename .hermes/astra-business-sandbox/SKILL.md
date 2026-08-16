---
name: astra-capabilities
description: Use Astra semantic capabilities when a user requests an authoritative external fact or governed action.
---

# Astra semantic capabilities

Astra tools provide authoritative external capabilities. Use the matching
Astra capability when the task requires governed external data or effects.

The capability result is the authoritative answer and may include receipt or
evidence references. Do not manage Astra Tasks, leases, approvals, retries,
or completion state from the Agent-facing surface. Those Runtime concerns are
handled behind the capability broker.

Ordinary Hermes tools and Skills remain available for non-Astra work.
