# Astra Documentation Authority

> **Documentation Governance**
> - **Role:** Repository-wide authority manifest and document navigation root.
> - **Authority:** A0 — Documentation Governance.
> - **Topic:** GOVERNANCE.DOCUMENTATION
> - **Scope:** Assigns document roles, topic ownership, precedence, status, and dependency direction.
> - **Not Responsible For:** Product requirements, architecture decisions, runtime semantics, remediation findings, or acceptance results.
> - **Depends On:** NONE
> - **Status:** FROZEN

This is the only repository-wide documentation authority manifest. It does not
change the product or architecture. It tells readers which existing document is
allowed to define each kind of information.

## 1. Authority model

The levels below describe responsibility, not a license for a document to
rewrite a different level:

| Level | Responsibility | Rule |
|---|---|---|
| A0 — Documentation Governance | Document roles, status, navigation, and conflict routing | This file is the only authority for deciding which document to read. |
| A1 — Product Definition | Product context, users, terminology, goals, scope, requirements, success standards, and product-level traceability | Product Definition does not override accepted ADRs or detailed runtime contracts. |
| A2 — Architecture / ADR | Cross-component boundaries and accepted architectural decisions | An accepted ADR owns the decision in its stated scope. |
| A3 — Runtime Design | Detailed normative contracts and runtime mechanics | The single Topic Authority listed below is authoritative; a more specific contract refines, but does not contradict, its ADR. |
| A4 — Remediation | Baseline-specific defects, corrective work, and closure conditions | A remediation document diagnoses drift from A1–A3; it cannot create or override product or design requirements. |
| A5a — Engineering Acceptance | Design- and implementation-level acceptance IDs, assertions, and evidence | Verifies a bounded design scope; it cannot invent missing semantics. A design gap returns to A1–A3. |
| A5b — Production / Release Acceptance | Release eligibility, operational evidence, and release decision | Reserved until a Product Definition or accepted ADR defines release policy; it is not supplied by Engineering Acceptance or Remediation. |

README, status, operations guides, research notes, source material, generated
files, and upstream Hermes documentation are **informational**. They never win a
conflict with A1–A5b.

## 2. Topic governance and conflict resolution

The following rules are mandatory for every current and future document:

1. A Topic has exactly one formal Authority. The registered source owns the
   normative definition; all other documents link to it and may only summarize
   it without adding, narrowing, or redefining semantics.
2. Authority follows Topic, not a filename, phase number, document date,
   “frozen” label, priority label, or document length. Identify the Topic in the
   registry before interpreting a requirement.
3. A proposed document must register its Topic ID, Authority level, owner,
   scope, and direct dependencies here before it introduces normative language.
   `Depends On` is a comma-separated list of registered Topic IDs only; document
   names, file paths, prose labels, and indirect dependencies are prohibited.
   `NONE` is allowed only for the A0 root Topic. If an
   existing Topic is affected, amend its owner rather than create a parallel
   specification.
4. An accepted ADR controls architecture. Its linked Runtime Design document
   controls detailed behavior within that decision; neither may contradict the
   Product Definition in its assigned scope.
5. Same-level dependencies must be one-way and acyclic. `Depends On` names only
   direct normative predecessors, not every related document. Cross-references
   are permitted but do not transfer authority.
6. Remediation and Acceptance may expose a conflict or gap but may not silently
   resolve it. Resolution requires an explicit change to the owning A1–A3
   source and, for an architectural decision, a new or superseding ADR.
7. Historical, Informational, Reference, Guide, generated, and upstream
   documents are explanation only. They must not define implementation behavior
   or be used as implementation authority.
8. `docs/PHASE_STATUS.md` reports implementation claims only. It never changes
   a requirement, design, remediation finding, or acceptance criterion.

The former phase-based priority statement in the root remediation plan is
superseded by these topic-based rules. Phase 4 is not globally “higher” than
Phase 3: Phase 3 owns governance-domain semantics; Phase 4 owns durable runtime
mechanics.

## 3. Registered Topic Authorities

The table is the repository's Topic registry. A row identifies one and only one
normative owner. “Supporting context” is deliberately non-normative.

| Topic ID | Topic | Single authoritative source | Supporting context |
|---|---|---|
| GOVERNANCE.DOCUMENTATION | Documentation Governance | `docs/AUTHORITY.md` | None |
| PRODUCT.CORE | Product context, goals, scope, requirements, and success standards | `docs/PRD.md` | `README.md`, `docs/PHASE_STATUS.md`, `docs/history/development_updated.md` |
| ARCH.HERMES_ASTRA_BOUNDARY | Hermes/Astra reliability ownership boundary | `docs/adr/ADR-003-task-reliability-boundary.md` | `docs/history/HERMES_ARCHITECTURE_ANALYSIS.md` |
| ARCH.HERMES_ASTRA_LEARNING_INTEGRATION | Hermes/Astra Learning Artifact integration boundary | `docs/adr/ADR-005-hermes-learning-artifact-integration.md` | ADR-003, `docs/design/HERMES_SELF_IMPROVING_LOOP.md` |
| ARCH.PERSISTENT_TASK_RUNTIME | Persistent Task Runtime architecture decision | `docs/adr/ADR-004-persistent-task-runtime.md` | Phase 3 contracts |
| DESIGN.GOVERNANCE.TASK_CONTRACT | Task Contract | `docs/phase3/TASK_CONTRACT.md` | ADR-003 |
| DESIGN.GOVERNANCE.EFFECT_IDENTITY | Canonical effect identity and idempotency | `docs/phase3/EFFECT_IDENTITY_AND_IDEMPOTENCY.md` | Task Contract, Approval Binding |
| DESIGN.GOVERNANCE.APPROVAL | Approval binding | `docs/phase3/APPROVAL_BINDING.md` | Effect Identity, Task Lifecycle |
| DESIGN.GOVERNANCE.LIFECYCLE | Task/Attempt/Execution/Interaction domain lifecycle | `docs/phase3/TASK_LIFECYCLE.md` | ADR-003 |
| DESIGN.GOVERNANCE.EVIDENCE | Authoritative records, facts, outbox, evidence, snapshots | `docs/phase3/RELIABILITY_FACTS_AND_EVIDENCE.md` | Task Contract, Effect Identity |
| DESIGN.GOVERNANCE.RULES | Task Rules | `docs/phase3/TASK_RULES.md` | Evidence contract |
| DESIGN.GOVERNANCE.POLICY | Task Policy | `docs/phase3/TASK_POLICY.md` | Task Rules, Decision Points |
| DESIGN.GOVERNANCE.COMPLETION | Requirement evaluation and completion aggregation | `docs/phase3/REQUIREMENT_EVALUATORS.md` | Task Contract, Evidence contract |
| DESIGN.GOVERNANCE.DECISIONS | Decision points, snapshot use, decision idempotency, CAS | `docs/phase3/DECISION_POINTS_AND_SNAPSHOTS.md` | Lifecycle, Evidence, Completion |
| DESIGN.INTEGRATION.HERMES_GOVERNED_EXECUTION | Public Hermes/Astra exchange for one governed Execution | `docs/design/HERMES_ASTRA_GOVERNED_EXECUTION_INTEGRATION.md` | ADR-005, Evidence contract |
| DESIGN.RUNTIME.DURABLE_TASK | Durable scheduling, waiting, cancellation, checkpoint, recovery, lease/fencing | `docs/adr/ADR-004-persistent-task-runtime.md` | Phase 3 contracts |
| ACCEPTANCE.ENGINEERING.P4 | Phase 4 frozen engineering acceptance IDs and matrix | `docs/phase4/ACCEPTANCE_MATRIX.md` | Phase 4 Runtime Design |

## 4. Document lifecycle and change governance

### Lifecycle

Every Authority document uses exactly one lifecycle state in its Header:

```text
Draft → Review → Frozen → Superseded → Historical
```

- **Draft:** proposed and not an implementation authority.
- **Review:** under named review; not an implementation authority unless an
  existing Frozen Authority explicitly delegates a bounded experiment.
- **Frozen:** the current implementation authority for its Topic.
- **Superseded:** replaced by a named Frozen successor; never an implementation
  authority.
- **Historical:** retained for provenance only; never an implementation
  authority.

`Accepted` is an ADR decision outcome, not a lifecycle state. Informational
documents retain their own freshness labels (for example `CURRENT` or
`HISTORICAL REFERENCE`) and are never Authority documents.

### Frozen Authority changes

Editorial corrections that do not alter behavior, contract semantics, ownership,
scope, or evidence requirements may amend a Frozen document directly. Any
behavior, Contract, trust/ownership boundary, durability/recovery guarantee, or
acceptance-semantic change must use this sequence:

1. Create or revise a Draft successor and identify the affected Topic IDs.
2. Create a new ADR when the change creates or reverses an architectural
   decision; revise the existing ADR only when the decision remains unchanged.
   A changed decision supersedes the earlier ADR.
3. Review and freeze the successor only after synchronizing the Topic Registry,
   affected downstream Design Topics, and corresponding Engineering Acceptance.
4. Mark the predecessor `Superseded` and link it to the Frozen successor; retain
   it for provenance. Update release acceptance as well when A5b exists and the
   release claim is affected.

No code, acceptance result, remediation record, status report, or guide may
silently redefine a Frozen Authority. Change review must reject a Frozen Topic
change whose registry, declared dependencies, or required downstream impact
updates are absent.

## 5. Extension slots and document creation rules

The hierarchy is intentionally stable: new capabilities extend the Topic
registry; they do not require a new Authority level. The following reserved
Topic IDs have **no current Authority** and therefore must not be treated as
specified behavior:

| Reserved Topic ID | Intended future scope | Registration precondition |
|---|---|---|
| DESIGN.RUNTIME.WORKFLOW | Workflow Runtime, if product scope changes | A1 scope decision and an A2 ADR before Runtime Design. |
| DESIGN.RUNTIME.BROWSER | Browser Runtime and its execution/safety boundary | A1 scope decision; an A2 ADR if it changes execution ownership or trust boundaries. |
| DESIGN.INTEGRATION.PROVIDER | Provider framework, model/provider adapters, and provider resilience | A2 ADR when it changes Hermes/Astra or external-provider boundaries. |
| DESIGN.RUNTIME.SCHEDULER | Scheduler policy and queue semantics beyond the current durable runtime | A2 ADR if it changes ownership, durability, or fairness guarantees. |
| DESIGN.RUNTIME.RECOVERY | Recovery orchestration beyond the existing bounded mechanics | Must refine, not duplicate, `DESIGN.RUNTIME.DURABLE_TASK`. |
| DESIGN.RUNTIME.MULTI_NODE | Multi-node coordination and distributed runtime behavior | A2 ADR before any design contract. |
| ACCEPTANCE.ENGINEERING.P5 | Phase 5 engineering acceptance | Registered only with its owning Runtime/Design Topic. |
| ACCEPTANCE.RELEASE | Production/release acceptance and release decision | A5b; requires a Product or ADR-defined release policy. |

When a reserved Topic becomes active, create one designated source under the
appropriate area (for example `docs/design/`, `docs/adr/`, or
`docs/acceptance/`) and add its row here in the same change. Do not create
parallel “overview”, “spec”, and “phase” documents that all define the topic.

### Remediation organization

The current root-level remediation record remains valid for backward
compatibility. Future remediation cycles must use
`docs/remediation/YYYY-MM-<scope>.md` and receive a unique
`REMEDIATION.<cycle>.<scope>` Topic ID. Each record must state its audit
baseline, disposition (`active`, `superseded`, `closed`, or `historical`), and
the A1–A3 Topics it evaluates. A remediation index may be added only when at
least two such records exist; it is navigation, never an authority source.

## 6. Document registry (derived navigation view)

The Topic Registry above is the sole normative mapping from Topic ID to source
document. This table is a derived navigation view: it must contain every active
Topic source exactly once, but it does not independently assign Topic, Authority,
or lifecycle state. The Documentation Governance Check verifies that it remains
consistent with the Topic Registry.

### Current normative documents

| Document | Role | Authority | Status |
|---|---|---|---|
| `docs/AUTHORITY.md` | Documentation Governance | A0 | FROZEN |
| `docs/PRD.md` | Product Requirements Document | A1 | FROZEN |
| `docs/adr/ADR-003-task-reliability-boundary.md` | Architecture decision | A2 | FROZEN |
| `docs/adr/ADR-005-hermes-learning-artifact-integration.md` | Learning Artifact integration boundary | A2 | FROZEN |
| `docs/adr/ADR-004-persistent-task-runtime.md` | Architecture decision | A2 | FROZEN |
| `docs/phase3/TASK_CONTRACT.md` | Runtime Design — Task Contract | A3 | FROZEN |
| `docs/phase3/EFFECT_IDENTITY_AND_IDEMPOTENCY.md` | Runtime Design — effect identity | A3 | FROZEN |
| `docs/phase3/APPROVAL_BINDING.md` | Runtime Design — approval | A3 | FROZEN |
| `docs/phase3/TASK_LIFECYCLE.md` | Runtime Design — domain lifecycle | A3 | FROZEN |
| `docs/phase3/RELIABILITY_FACTS_AND_EVIDENCE.md` | Runtime Design — evidence authority | A3 | FROZEN |
| `docs/phase3/TASK_RULES.md` | Runtime Design — rules | A3 | FROZEN |
| `docs/phase3/TASK_POLICY.md` | Runtime Design — policy | A3 | FROZEN |
| `docs/phase3/REQUIREMENT_EVALUATORS.md` | Runtime Design — completion evaluation | A3 | FROZEN |
| `docs/phase3/DECISION_POINTS_AND_SNAPSHOTS.md` | Runtime Design — decisions and CAS | A3 | FROZEN |
| `docs/design/HERMES_ASTRA_GOVERNED_EXECUTION_INTEGRATION.md` | Runtime Design — governed Execution integration | A3 | FROZEN |
| `docs/phase4/ACCEPTANCE_MATRIX.md` | Phase 4 Engineering Acceptance | A5a | FROZEN |

### Informational and reference documents

| Document or path | Role | Status / restriction |
|---|---|---|
| `README.md` | Repository landing page | CURRENT; navigation and summary only |
| `docs/PHASE_STATUS.md` | Current implementation status register | CURRENT; non-normative |
| `docs/history/HERMES_ARCHITECTURE_ANALYSIS.md` | Version-scoped research record | HISTORICAL REFERENCE; Hermes 0.18.2 facts only |
| `docs/history/development_updated.md` | Former combined product/design planning source | HISTORICAL REFERENCE; superseded by `docs/PRD.md` |
| `docs/history/EXECUTION_TYPE_RUNTIME_IMPLEMENTATION_PLAN.md` | Execution-type implementation plan | HISTORICAL REFERENCE; superseded by current CLI and Runtime Design |
| `docs/history/PRODUCT_EVOLUTION_ROADMAP.md` | Future-stage hypotheses | HISTORICAL REFERENCE; does not authorize implementation |
| `docs/深入理解-AI-Agent-李博杰-v1.txt` | External background source | REFERENCE ONLY; not Astra authority |
| `hermes-agent-main/**` | Vendored upstream source and documentation | UPSTREAM REFERENCE; governed by Hermes, not Astra |
| `.pytest_cache/**` and generated outputs | Tool-generated material | GENERATED; never documentation authority |

## 7. Dependency direction

```text
AUTHORITY
└── Product Definition
    ├── Architecture / ADR
    │   └── Runtime Design
    │       ├── Remediation
    │       ├── Engineering Acceptance
    │       └── Production / Release Acceptance (reserved)
    └── Status / README / Operations / Research (informational projections)
```

Normative documents may depend only on levels above them or on an explicitly
named same-level Topic predecessor. The resulting graph must be acyclic.
Informational documents must link back to the registered Topic Authority instead
of restating new requirements. Markdown links must resolve within this repository
or be explicitly labeled as upstream/external; references to absent documents
are governance defects, not implicit future authorities.

## 8. Documentation Governance Check

Run the read-only check before merging changes to governed documents:

```bash
python scripts/check_documentation_governance.py
```

The repository CI runs the same command for changes to governed documents.

It verifies Header completeness, controlled lifecycle states, Topic ID
uniqueness, Topic Registry and derived Document Registry consistency, Topic-ID
dependencies, dependency acyclicity, local document-reference validity, and the
prohibition on Informational documents declaring A0–A5b Authority.

## 9. Known unresolved governance issues

- Repository-wide quantitative reliability thresholds remain a documented
  design gap. No status, test, remediation, or acceptance document may invent
  them until the Product Definition or an accepted design decision owns them.
- `docs/phase4/ACCEPTANCE_MATRIX.md` is A5a Engineering Acceptance only. A5b
  Production / Release Acceptance is intentionally reserved: the remediation
  plan's BV items are closure checks for its audit findings, not a substitute
  for release policy or a general release specification.
