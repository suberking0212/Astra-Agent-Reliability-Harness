# Stage 1 Governed Business Execution Closure

## Boundary

`Hermes Agent -> Astra Business Protocol -> Astra Runtime service -> Task /
Attempt / Execution / Lease / Governance / Tool Gateway -> Business Sandbox`.

Hermes has no Runtime mechanics: no execution token or ID, attempt ID, lease
owner/token, fencing token, or internal identity.  It supplies `task_id` and
business arguments only.  The service owns the authoritative SQLite Runtime,
claims and heartbeats leases internally, and creates the Gateway invocation.
It returns only business state and a high-level next action.

`task_id` is a correlation locator, never an authorization credential.  The
Plugin authenticates to the service with a per-launch machine transport secret
in the `X-Astra-Plugin-Auth` header.  That secret is deliberately absent from
tool schemas, prompts, tool results, and (after plugin registration) Hermes'
process environment, so terminal children cannot inherit it. After caller authentication, the
Runtime alone resolves Task to the current legal Execution and checks its
internal lease/fencing state before Gateway dispatch.

The supported protocol is `astra_submit_task`, `astra_runtime_status`,
`astra_resolve_input`, `astra_resolve_approval`, `astra_cancel_task`, and
`astra_business_<operation>`.  The Runtime persists approval and external
operation facts before any effect; reconciliation observes an indeterminate
operation rather than replaying it.  Task success remains conditioned on the
Completion Contract's authoritative evidence, never Hermes prose.

## Resource isolation

`scripts/start_astra_cli.sh` starts the Sandbox and Runtime as separate owner
processes, strips Sandbox credential variables before launching Hermes, then
uses macOS `sandbox-exec`.  The sandbox profile denies reads/writes to the
authoritative Sandbox SQLite file and denies direct outbound access to the raw
Sandbox listener.  It fails closed (exit 78) if `sandbox-exec` is unavailable
or rejects the generated profile.  This is an OS enforcement boundary, not a
prompt/tool-hiding convention.  The Runtime loopback API remains the sole
business route.

## Acceptance coverage

The black-box Hermes CLI test verifies the public plugin discovers and calls
only the Runtime service with no Sandbox credentials in its environment.
Persistent Runtime/Gateway tests cover approval gating, committed-response-loss
reconciliation, and Completion Contract rejection.  Assertions inspect task,
interaction, operation, receipt, reconciliation, and business persistence;
they do not accept model text as evidence.

## Deliberately unfinished

- Stage 2A Session ↔ Task correlation and resume correlation are complete. The
  Astra-owned correlation store binds the public Hermes CLI session ID to the
  authoritative Astra task ID after successful submission; conflicting or
  ambiguous mappings fail closed. This informational document does not redefine
  the normative boundary or add native Hermes lifecycle semantics.
- Native Hermes pause/resume (requires the same public API).
- Approval UI callback into Hermes (requires public interactive callback
  support and Session-to-Task binding).
- Learning / Memory / Skill / Curator / Artifact integrations and ADR-005
  Layer 1-3 (frozen by scope).
