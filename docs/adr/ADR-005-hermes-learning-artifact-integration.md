# ADR-005: Hermes Learning Artifact Integration Boundary

> **Documentation Governance**
> - **Role:** Accepted architecture decision for the Hermes/Astra Learning Artifact integration boundary.
> - **Authority:** A2 — Architecture / ADR.
> - **Topic:** ARCH.HERMES_ASTRA_LEARNING_INTEGRATION
> - **Scope:** Ownership, permitted exchange, and prohibitions for integrating Hermes-native Learning Artifacts with one Astra-governed Execution.
> - **Not Responsible For:** Hermes Artifact Lifecycle, Memory/Skill/Curator implementation, Runtime mechanics, schemas, evaluation, acceptance, or implementation status.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, PRODUCT.CORE, ARCH.HERMES_ASTRA_BOUNDARY
> - **Status:** FROZEN

> Decision: `ACCEPTED / 2026-08-06`  
> Extends: `ADR-003-task-reliability-boundary.md` within the narrower Learning
> Artifact integration scope. It does not replace ADR-003's Task Reliability
> boundary.

## Decision

### Hermes owns

Hermes is the sole source of truth for Memory, Skill, Curator, Learning,
Artifact content and native version, Artifact Lifecycle, retrieval, matching,
merge, native storage, and prompt assembly.

### Astra owns

Astra owns Task/Attempt/Execution facts, Task Contract and execution-boundary
governance, evidence, and audit. For a Hermes Artifact, Astra may retain only:

- a content-free Evidence Association; and
- an Execution Admission Decision bound to one Task, Attempt, Execution, and
  native Artifact reference.

An Execution Admission Decision is an Astra audit/governance fact for that one
governed Execution. It creates no Artifact-level or global status, and never
changes a Hermes Artifact or its native lifecycle.

### Permitted exchange

The only integration exchange is through Hermes public, supported interfaces:

```text
Astra Execution Boundary
→ Hermes Execution
→ Hermes Artifact Used Observation
→ Astra Evidence Association
→ Astra Execution Admission Decision / audit
```

Astra supplies an execution boundary for the submitted Task; it does not choose
or match an Artifact. Hermes decides whether to retrieve or use a native
Artifact and, when it does, reports the native Artifact reference/version and
provenance. Astra associates that observation with governed evidence and the
single Execution's decision/audit record.

### Prohibitions and public-interface gate

Astra must not schedule, configure, or invoke Hermes Curator; own Artifact
content or lifecycle; perform Artifact retrieval, matching, merge, or prompt
assembly; or use private APIs, monkey-patches, file scanning, or any workaround
to infer, control, or alter Hermes learning behavior.

Layer 1–3 integration is blocked until Hermes exposes all of the following as
public, supported interfaces:

1. Task-bound Execution Boundary input;
2. observation of the native Artifact reference/version actually used; and
3. Artifact provenance.

Until then, Astra retains only its existing content-free Evidence Association
capability and does not begin Learning Artifact integration.
