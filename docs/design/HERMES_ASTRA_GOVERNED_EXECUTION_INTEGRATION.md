# Hermes ↔ Astra Governed Execution Integration Contract

> **Documentation Governance**
> - **Role:** Normative integration contract for one Astra-governed Hermes Execution.
> - **Authority:** A3 — Runtime Design / Integration Contract.
> - **Topic:** DESIGN.INTEGRATION.HERMES_GOVERNED_EXECUTION
> - **Scope:** Public-interface exchange, responsibilities, and order for a Hermes Learning Artifact used in one governed Execution.
> - **Not Responsible For:** Hermes Artifact Lifecycle; Memory, Skill, Curator, Learning, retrieval, matching, merge, or prompt-assembly internals; Runtime mechanics; schemas; recovery; evaluation; acceptance; or implementation status.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.HERMES_ASTRA_BOUNDARY, ARCH.HERMES_ASTRA_LEARNING_INTEGRATION, DESIGN.GOVERNANCE.EVIDENCE
> - **Status:** FROZEN

## 1. Applicability

This contract applies only after Hermes provides the public interfaces required
by ADR-005. Until then, no Layer 1–3 integration is implemented.

## 2. Exchange and order

```text
Astra Execution Boundary
→ Hermes Execution
→ Hermes Artifact Used Observation
→ Astra Evidence Association
→ Execution Admission Decision / Audit
```

| Step | Sender | Exchange | Responsibility |
|---|---|---|---|
| 1 | Astra | Execution Boundary for one Task/Attempt/Execution | Supplies Task Contract and execution-boundary constraints; does not select an Artifact. |
| 2 | Hermes | Execution | Performs native reasoning and independently decides whether to retrieve or use an Artifact. |
| 3 | Hermes | Artifact Used Observation | Reports the native Artifact reference/version actually used and its provenance through a public interface. |
| 4 | Astra | Evidence Association | Associates the observation with the one Task/Attempt/Execution and applicable evidence; stores no Artifact content or native state. |
| 5 | Astra | Execution Admission Decision / audit | Records the governed-use decision for that one Execution only; it does not create Artifact status or modify Hermes. |

## 3. Public-interface requirements

Hermes must provide public, supported interfaces for:

1. accepting the Task-bound Execution Boundary;
2. returning the Artifact Used Observation, including native reference/version;
   and
3. returning Artifact provenance.

Absent any one interface, Astra must not integrate Layer 1–3 and must not use a
private API, monkey-patch, file scan, or inferred Artifact state as a substitute.

## 4. Boundary

Hermes remains the only owner of all native Learning Artifact behavior. Astra
remains the owner of governed Task facts and evidence. An Execution Admission
Decision is scoped to one Task/Attempt/Execution and native Artifact reference;
it expires with that Execution and is not an Artifact Lifecycle state.
