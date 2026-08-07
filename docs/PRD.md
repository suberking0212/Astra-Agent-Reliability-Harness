# Astra Agent Reliability Harness — Product Requirements Document

> **Documentation Governance**
> - **Role:** The repository's single Product Requirements Document and Product Definition.
> - **Authority:** A1 — Product Definition.
> - **Topic:** PRODUCT.CORE
> - **Scope:** Product purpose, users, terminology, goals, scope, requirements, success standards, risks, and product-level traceability.
> - **Not Responsible For:** Architecture decisions, runtime design, algorithms, schemas, state machines, implementation plans, engineering acceptance, release acceptance, or current delivery status.
> - **Depends On:** GOVERNANCE.DOCUMENTATION
> - **Status:** FROZEN

Document version: `1.3`  
Effective date: `2026-08-06`  
Supersedes: `docs/history/development_updated.md` as `PRODUCT.CORE`

## 1. Executive Summary

Astra Agent Reliability Harness is a research, validation, and demonstration
product that serves as the Reliability & Governance Layer for Hermes. Hermes is
the Agent Runtime: it supplies the autonomous execution and native learning
capabilities. Astra operates above that Runtime at the Task level, making
business work durable, governable, verifiable, recoverable, and auditable.

Astra exists because an Agent completing a reasoning loop or claiming success
does not by itself prove that a business task is complete, authorized, safe, or
recoverable. The product therefore maintains task-level truth, constrains
controlled actions, verifies outcomes using evidence, supports interruption and
recovery, and evaluates reliability independently of the Agent's own claims.

The product is intended to demonstrate autonomous work with deterministic
reliability, safety, and task-level accountability.

In addition to governed task execution, Astra supports evidence-governed
adaptive improvement through Hermes' native learning capabilities. As defined
in the Product Boundary, Astra applies evidence, governance, evaluation,
promotion, audit, and rollback boundaries to learning artifacts.

## 2. Product Context

### 2.1 Why Astra exists

Autonomous Agents can interpret goals, choose tools, react to results, and
replan. Those capabilities are necessary but insufficient for reliable business
execution. Long-running or side-effecting tasks introduce product risks that an
individual Agent turn cannot resolve alone:

- a natural-language completion claim may not match actual business state;
- a retry, restart, or concurrent execution may repeat an external side effect;
- required information or approval may be missing;
- a task may be left suspended or ambiguous after process failure;
- cancellation may arrive while work is still in progress;
- execution logs may be insufficient to establish what actually happened;
- reliability improvements may be overstated without an independent comparison.

Astra addresses these risks at the Task level while preserving the Agent's
ability to decide how to perform the work.

### 2.2 Relationship with Hermes

The Product Boundary defines the respective responsibilities of Hermes and
Astra. Hermes integration preserves that boundary: Astra governs Task-level
recognition and controlled action without prescribing a fixed business plan.

For learning artifacts, Astra governs the associated evidence, provenance,
evaluation, promotion eligibility, audit, and rollback boundary. It can reject,
disable, or roll back an artifact when applicable evidence does not support
continued use.

Learning artifacts do not create new Task authority. Memory or Skills may
inform Hermes execution, but they cannot expand a Task Contract, grant a
controlled capability, replace required approval, or determine Task completion
without the same Astra governance and evidence requirements applied to other
Agent execution.

### 2.3 Product Boundary

The product boundary is intentional: Hermes is the Agent Runtime and Astra is
the Reliability & Governance Layer on top of it.

| Ownership | Responsibilities |
|---|---|
| **Hermes owns** | Agent Runtime; Reasoning; Planning; Conversation; Tool Framework and Tool Registry; Prompt Assembly; Provider framework; CLI; Memory; Skills; Curator; and Native Learning. Hermes decides how to perform work within the constraints supplied to it. |
| **Astra owns** | Task Runtime; Task Contract; Task truth; authority and capability governance; approvals; evidence; completion; recovery and reconciliation; audit; evaluation; Business Sandbox; and the governance, provenance, promotion, and rollback boundaries applied to Hermes learning artifacts. Astra decides what may be authorized, recognized, continued, or completed at the Task level. |

The boundary does not prevent Astra from integrating with, governing, or
extending Hermes capabilities; integration preserves Hermes as the owning
Runtime.

### 2.4 What Astra is not

Astra is not:

- a duplicate of Hermes-owned Agent capabilities, including Agent Runtime,
  Tooling, Prompt Assembly, Memory, Skills, Native Learning, Provider
  Integration, and CLI;
- a Workflow Engine, DAG runner, or Workflow Builder;
- a multi-Agent orchestration platform;
- a general AI operating system;
- a model-training platform;
- an enterprise organization, marketplace, or workforce-management product;
- a generic browser or computer-use product;
- a substitute for the external business systems that own business state.

See Product Boundary for the responsibility definition.

## 3. Product Terminology

These definitions freeze product meaning only. Detailed contracts and lifecycle
semantics belong to their registered Architecture and Design Topics.

| Term | Product meaning |
|---|---|
| Task | A durable unit of user-requested work whose authoritative outcome is governed by Astra. |
| Task Contract | The product-level agreement describing the Task's objective, permitted scope, controlled capabilities, required approvals, limits, and completion expectations. |
| Attempt | A bounded effort to achieve a Task outcome. A Task may require more than one Attempt without becoming a different user request. |
| Execution | One bounded Agent execution within an Attempt. |
| Interaction | A durable request for information or approval that must be resolved before governed work can continue. |
| Approval | Explicit authorization for a controlled action within the Task's permitted scope. |
| Effect | A business action that can change an external system or produce an externally meaningful consequence. |
| Evidence | Information that can support or refute a product-level claim about task progress, authorization, external effects, or completion. |
| Completion | The product decision that the Task's declared completion expectations have been satisfied by acceptable evidence. |
| Governance | Deterministic task-level controls and decisions that determine what may be recognized, allowed, continued, escalated, or completed. |
| Recovery | Returning an interrupted Task to a safe and explainable product state without assuming that in-memory execution survived. |
| Reconciliation | Resolving uncertainty by comparing Astra's task facts with the relevant external business facts. |
| Trace | A chronological record used for observation, diagnosis, and evaluation; it is not automatically authoritative business truth. |
| Evaluation | A controlled assessment of task outcomes, safety, reliability, and efficiency across comparable system configurations. |
| Learning Artifact | A product-recognized adaptive artifact such as a Memory or Skill, created or changed through Hermes' native learning lifecycle and subject to Astra evidence, governance, evaluation, audit, and rollback boundaries. Its existence does not establish correctness or publication eligibility. |
| Memory Candidate | Memory content that may be considered for durable future use but has not yet been supported for its intended scope by sufficient provenance and evidence. |
| Skill Candidate | A Skill created or changed through Hermes' native lifecycle that has not yet received a governed promotion decision for ordinary use. |
| Provenance | The attributable origin of a Learning Artifact, including the applicable execution profile, Hermes Home or session context, generation source, and available Task or evaluation evidence. |
| Promotion | A governed decision that a Learning Artifact is eligible for use within its declared scope; it does not expand Task authority or imply universal correctness. |
| Rollback | A governed action that prevents a promoted Learning Artifact version from continuing to influence applicable future execution while preserving its history and evidence. |
| Release | A declared product capability set whose eligibility is determined outside this PRD by the registered Release Acceptance Authority. |

## 4. Users and Stakeholders

| User or stakeholder | Product need |
|---|---|
| Agent reliability researcher | Compare reliability approaches using repeatable tasks and credible outcome evidence. |
| Developer or integrator | Connect Hermes, tools, business services, and reliability capabilities without ambiguous ownership. |
| Operator | Submit, inspect, resume, approve, cancel, and diagnose Tasks through supported product interfaces. |
| Reviewer or auditor | Determine what the Agent attempted, what Astra allowed, what external effects occurred, and why a Task reached its outcome. |
| Demonstration user | Observe successful execution, controlled failure handling, human interaction, recovery, and outcome verification. |
| Product or release owner | Decide whether a defined capability is sufficiently evidenced for its intended release claim. |

## 5. Product Principles

1. The Agent remains autonomous within declared capabilities and constraints.
2. Task completion is evidence-based and contract-based, not based solely on
   the Agent's self-assessment.
3. Controlled external effects fail closed when authorization, scope, identity,
   or required evidence is missing or ambiguous.
4. Task truth remains durable across turns, executions, attempts, process
   restarts, and supported concurrency.
5. Human input and approval are durable product interactions, not simulated
   text responses.
6. Observation must not silently become control, and advisory feedback must not
   become an undeclared execution plan.
7. Evaluation must be sufficiently independent to avoid treating the evaluated
   system's own success claim as ground truth.
8. Product claims must be narrower than or equal to the available evidence.
9. Missing product or design semantics must remain explicit gaps rather than
    being invented by implementation or tests.

## 6. Product Goals

| ID | Goal | Success meaning |
|---|---|---|
| GOAL-001 | Enable autonomous completion of real, tool-using business tasks. | The Agent can interpret a Task, use permitted capabilities, adapt to results, request needed interaction, and produce an outcome. |
| GOAL-002 | Establish durable Task truth. | Task progress and outcome remain explainable across multiple Executions, Attempts, and supported restarts. |
| GOAL-003 | Govern external effects safely. | Controlled effects are scoped, authorized, attributable, and protected against unsafe repetition. |

| GOAL-004 | Prevent false completion. | A Task is not recognized as complete without evidence that satisfies its declared expectations. |
| GOAL-005 | Support interruption, cancellation, and recovery. | A Task can reach an explainable continuation, waiting, reconciliation, cancellation, or terminal outcome after disruption. |
| GOAL-006 | Make execution auditable. | Reviewers can distinguish Agent statements, Astra decisions, and external business facts. |

| GOAL-007 | Measure reliability credibly. | Comparable configurations can be assessed using consistent tasks, product metrics, and outcome evidence. |
| GOAL-008 | Demonstrate the product clearly. | Users can observe the Task lifecycle, controlled interactions, effects, evidence, recovery, and evaluation results. |
| GOAL-009 | Enable evidence-governed adaptive improvement. | Hermes can accumulate, maintain, and use Memory and Skills within independently supported evidence and governance boundaries, while unsafe, unsupported, or degrading learning remains observable and reversible. |

## 7. Product Scope

### 7.1 In Scope

- integration with Hermes as the autonomous Agent execution capability;
- governance and extension of Hermes capabilities in accordance with the
  Product Boundary;
- durable Task, Attempt, Execution, and Interaction concepts;
- task-level contracts, constraints, limits, governance, and completion;
- controlled tool access and external-effect governance;
- user-input and approval interactions;
- evidence collection, outcome verification, and reconciliation;
- cancellation, interruption handling, durable continuation, and supported recovery;
- supported scheduling and concurrency safety;
- observable and auditable task execution;
- deterministic business simulation and fault injection;
- comparative reliability evaluation;
- evidence-governed integration of Hermes native Memory, Skills, Curator, and
  learning lifecycle;
- evidence-governed observation of adaptive improvement;
- Learning Artifact provenance, evaluation, promotion decisions, audit, and
  versioned rollback boundaries;
- operator-facing task controls and inspection;
- a focused frontend and demonstration experience;
- replaceable business and provider integrations within the declared product
  boundaries.

### 7.2 Out of Scope

- duplicating Hermes-owned Agent capabilities defined in the Product Boundary;
- prescribing fixed business workflows or tool-call sequences;
- multi-Agent planning or coordination;
- general Workflow Engine or DAG capabilities;
- browser use, computer use, or autonomous UI operation;
- model training or fine-tuning;
- publication of Learning Artifacts without applicable provenance, evidence,
  evaluation, and a governed promotion decision;
- learning that expands Task Contract scope, controlled capabilities,
  approvals, or external-effect authority;
- a general marketplace, organization system, or employee registry;
- full enterprise tenancy, IAM, RBAC, or ABAC platforms;
- a general-purpose memory platform;
- arbitrary cross-host distributed execution without a future product and
  architecture decision;
- claiming production readiness without a registered Production / Release
  Acceptance Authority.

## 8. Product Journeys

### 8.1 Governed task completion

A user submits a business Task. The Agent determines how to act within the Task
Contract, uses permitted capabilities, and submits an outcome. Astra recognizes
completion only when the required product evidence is available.

### 8.2 Missing information

When required information is unavailable, the Task enters a durable waiting
condition. After the user supplies the information, work continues without
pretending that the original in-memory execution remained active.

### 8.3 Controlled approval

Before a controlled action, the product obtains an approval that is applicable
to the requested action. Approval for one action must not silently authorize a
different action.

### 8.4 Interruption and recovery

After an execution or service interruption, the product determines whether the
Task may continue, must wait, must reconcile uncertain business state, or must
terminate. Recovery must not create an unexplained duplicate business effect.

### 8.5 Cancellation

An operator cancels a Task through a supported product interface. The
cancellation becomes durable, later activity cannot overwrite the Task outcome,
and uncertain external effects remain visible for reconciliation.

### 8.6 Reliability evaluation

A researcher executes comparable Task sets under defined configurations and
receives results that distinguish actual business outcomes, safety failures,
recovery behavior, and execution efficiency.

### 8.7 Audit and demonstration

A reviewer or demonstration user inspects a Task and can understand the Agent's
actions, Astra's governance decisions, human interactions, relevant evidence,
external effects, and final product outcome.

## 9. Functional Requirements

Every formal requirement has one stable ID. The sections below organize the
existing requirements by responsibility domain. Category expresses its product
area; it does not create a separate numbering namespace or change the meaning
of a requirement.

### 9.1 Task Runtime and Hermes Integration

| ID | Category | Priority | Requirement |
|---|---|---|---|
| FR-001 | Task Management | Must | The product shall accept a Task with an explicit objective, permitted scope, applicable limits, interaction expectations, and completion expectations. |
| FR-002 | Task Management | Must | The product shall expose the authoritative current Task outcome and sufficient context to understand whether work is active, waiting, cancelled, completed, failed, or unresolved. |
| FR-003 | Agent Execution | Must | The product shall use Hermes for autonomous reasoning, tool selection, execution ordering, local adaptation, and response construction. |
| FR-004 | Agent Execution | Must | The product shall allow the Agent to adapt its execution plan within declared product constraints without requiring a predetermined workflow. |
| FR-005 | Product Boundary | Must | The product shall prevent Astra reliability components from becoming a second Agent loop or business planner. |

### 9.2 Governance

| ID | Category | Priority | Requirement |
|---|---|---|---|
| FR-006 | Contract | Must | The product shall preserve the Task Contract applicable to a Task and prevent undeclared changes from silently altering the Task's permitted meaning. |
| FR-007 | Capability Governance | Must | The product shall restrict controlled actions to capabilities and scope permitted for the Task. |
| FR-008 | Tool Governance | Must | The product shall mediate controlled business tools through a governed product boundary. |
| FR-009 | External Effects | Must | The product shall reject an external effect when its authorization, scope, identity, or required approval is missing, invalid, or ambiguous. |
| FR-010 | External Effects | Must | The product shall protect the same intended external effect from unsafe repetition across supported retries, Executions, Attempts, restarts, and concurrency. |
| FR-011 | External Effects | Must | The product shall retain enough attribution to connect a controlled effect to its Task, authorization, and relevant business outcome. |
| FR-012 | Human Interaction | Must | The product shall support durable requests for missing user information. |
| FR-013 | Human Interaction | Must | The product shall support durable requests for approval of controlled actions. |
| FR-014 | Human Interaction | Must | Resolving an Interaction shall allow the Task to continue or terminate according to current authoritative facts without assuming that the earlier Execution remains active. |

### 9.3 Reliability Runtime

| ID | Category | Priority | Requirement |
|---|---|---|
| FR-015 | Evidence | Must | The product shall distinguish Agent statements, execution observations, Astra governance decisions, and external business facts. |
| FR-016 | Completion | Must | The Agent may submit an outcome, but that submission alone shall not determine Task completion. |
| FR-017 | Completion | Must | The product shall evaluate declared completion expectations using acceptable evidence. |
| FR-018 | Completion | Must | The product shall prevent completion while required interaction, approval, external-effect confirmation, reconciliation, or completion evidence remains unresolved. |
| FR-019 | Completion | Must | The product shall preserve an already established business outcome when a later Agent response does not change the underlying authoritative facts. |
| FR-020 | Governance | Must | The product shall identify task-level reliability conditions that span Executions, Attempts, or supported restarts. |
| FR-021 | Governance | Must | The product shall select only bounded task-level actions and shall not use governance feedback to prescribe a hidden business workflow. |
| FR-022 | Limits | Must | The product shall enforce declared task-level limits and produce an explainable product outcome when a limit or deadline prevents continued work. |
| FR-023 | Cancellation | Must | The product shall support idempotent Task cancellation through a supported interface. |
| FR-024 | Cancellation | Must | Once cancellation is authoritative, later execution activity shall not replace the cancelled Task outcome or initiate a new controlled effect. |
| FR-025 | Recovery | Must | The product shall recognize Tasks left incomplete by supported process or service interruption. |
| FR-026 | Recovery | Must | The product shall classify interrupted work into a safe product outcome such as continue, wait, reconcile, fail, or cancel. |
| FR-027 | Recovery | Must | The product shall not repeat an uncertain external effect merely because the process that initiated it was interrupted. |
| FR-028 | Reconciliation | Must | The product shall support reconciliation when Astra's facts cannot conclusively establish the relevant external business outcome. |
| FR-029 | Scheduling | Must | The product shall maintain durable pending work and prevent non-executable or terminal Tasks from indefinitely blocking eligible work. |
| FR-030 | Concurrency | Must | The product shall prevent concurrent processing from simultaneously advancing the same governed unit of work under more than one valid authority. |
| FR-031 | Audit | Must | The product shall provide an ordered, queryable account of Task progress, interactions, controlled effects, evidence, governance decisions, and outcome. |
| FR-032 | Audit | Must | The product shall retain historical information required to explain superseded attempts, interrupted executions, late results, and reconciled uncertainty. |
| FR-033 | Operator Experience | Must | Operators shall be able to submit, inspect, resolve interactions for, cancel, and diagnose Tasks without directly modifying product storage. |
| FR-034 | Demonstration | Should | The product shall provide a focused visual experience showing Task progress, Agent activity, governed interactions, errors, recovery, evidence, and final outcome. |

### 9.4 Evaluation and Release Governance

| ID | Category | Priority | Requirement |
|---|---|---|
| FR-035 | Business Environment | Must | The product shall provide a deterministic business environment sufficient to demonstrate governed queries, tickets, refunds, replacements, notifications, and comparable business outcomes. |
| FR-036 | Business Environment | Must | The product shall support deterministic fault scenarios covering transient failures, invalid input, missing information, approval, cancellation, restart, duplicate-effect risk, and false-completion risk. |
| FR-037 | Integration | Must | Mock and future real business integrations shall present equivalent product capabilities without embedding simulation behavior into Astra's core product semantics. |
| FR-038 | Provider Compatibility | Should | Provider-specific differences shall not change Astra's product-level Task, governance, evidence, or completion meaning. |
| FR-039 | Evaluation | Must | The product shall support comparable evaluation of a Hermes baseline, a bounded retry configuration, and Hermes with Astra reliability capabilities. |
| FR-040 | Evaluation | Must | Comparable evaluations shall use equivalent tasks, product inputs, business conditions, and success definitions. |
| FR-041 | Evaluation | Must | Evaluation results shall not treat the evaluated Agent or reliability configuration's own completion claim as sufficient proof of success. |
| FR-042 | Evaluation | Must | The product shall report outcome, safety, recovery, interaction, and efficiency metrics using versioned evaluation definitions. |
| FR-043 | Release Governance | Must | Product status, engineering acceptance, and production/release eligibility shall remain distinct claims. |
| FR-044 | Release Governance | Must | The product shall not claim production readiness until a registered Production / Release Acceptance Authority defines and verifies the required evidence. |

### 9.5 Adaptive Learning Governance

| ID | Category | Priority | Requirement |
|---|---|---|
| FR-045 | Adaptive Learning | Must | The product shall integrate Hermes native Memory, Skills, Curator, and learning lifecycle without introducing a second Agent loop, Memory platform, Workflow Engine, or independent learning engine in Astra. |
| FR-046 | Memory | Must | The product shall enable Hermes native Memory only within an explicitly selected execution profile and an isolated Hermes Home. |
| FR-047 | Memory Provenance | Must | A Memory candidate considered for durable future use shall retain available provenance connecting it to its generation source, execution profile, Hermes Home or session context, and applicable Task or evaluation evidence. |
| FR-048 | Memory Governance | Must | Unsupported, conflicting, stale, sensitive, or unsafe Memory identified by applicable evidence shall not be silently promoted for future use. |
| FR-049 | Memory Authority | Must | Memory shall not expand Task Contract scope, controlled capabilities, approval authority, external-effect authority, or completion authority. |
| FR-050 | Memory Reversibility | Must | Promoted Memory shall remain attributable to a version and shall be inspectable, disableable, or rollback-capable through the governed learning boundary supported by Hermes and Astra. |
| FR-051 | Skill Provenance | Must | A Skill candidate shall retain available provenance connecting it to its generation source, execution profile, Hermes Home or session context, and applicable Task, completion, evaluation, or safety evidence. |
| FR-052 | Skill Evidence | Must | Creation or modification of a Skill artifact shall not by itself establish that learning occurred, that the Skill is correct, or that it is eligible for promotion. |
| FR-053 | Skill Eligibility | Must | A Skill candidate shall remain ineligible for governed promotion when required provenance, independently supported outcome evidence, or safety evidence is absent or ambiguous. |
| FR-054 | Skill Safety | Must | A Skill associated with false success, unsafe behavior, sensitive transfer, or incompatible scope shall not receive a governed promotion decision. |
| FR-055 | Skill Reversibility | Must | A promoted Skill shall remain attributable to a version and shall be inspectable, disableable, or rollback-capable through the governed learning boundary supported by Hermes and Astra. |
| FR-056 | Skill Reuse Evidence | Must | The product shall record available evidence of whether a promoted Skill was reused and the observed Task or evaluation outcome associated with that reuse. |

## 10. Non-Functional Requirements

| ID | Category | Priority | Requirement |
|---|---|---|---|
| NFR-001 | Safety | Must | Controlled actions shall fail closed when required product facts are missing, invalid, stale, or ambiguous. |
| NFR-002 | Correctness | Must | A Task outcome shall not contradict the authoritative business facts available to the product. |
| NFR-003 | Durability | Must | Task-level truth required for continuation, waiting, cancellation, recovery, and audit shall survive supported process restarts. |
| NFR-004 | Idempotency | Must | Repeating a supported command or recovery action shall not create a second logical product outcome for the same intent. |
| NFR-005 | Consistency | Must | Competing product actions shall not produce multiple authoritative terminal outcomes for one Task. |
| NFR-006 | Auditability | Must | Formal product claims shall be traceable to their Task, relevant evidence, governing requirement, and product decision. |
| NFR-007 | Reproducibility | Must | Defined evaluation and fault scenarios shall be repeatable under recorded product conditions. |
| NFR-008 | Isolation | Must | Core product semantics shall not depend on simulation data, frontend internals, or a specific business-system implementation. |
| NFR-009 | Compatibility | Must | Changes in Hermes or a Provider shall be contained so that product-level Task meaning and governance do not silently change. |
| NFR-010 | Observability | Must | Product users shall be able to distinguish active work, waiting, recovery, reconciliation, cancellation, completion, and failure. |
| NFR-011 | Security and Privacy | Must | Credentials, approvals, sensitive business data, and audit material shall be protected according to their risk and shall not be unnecessarily exposed in prompts, traces, or reports. |
| NFR-012 | Maintainability | Must | Product requirements shall remain traceable to one Topic Authority and shall not depend on duplicated normative definitions. |
| NFR-013 | Operability | Must | Supported product operations shall be available through documented interfaces and shall return explicit outcomes for invalid or unsafe requests. |
| NFR-014 | Performance | Should | The product shall expose latency, resource, and throughput behavior needed to establish future release targets without inventing thresholds in implementation or tests. |
| NFR-015 | Portability | Should | Core product meaning shall remain independent of a single model, Provider, mock service, or user interface. |
| NFR-016 | Learning Safety | Must | Learning Artifacts lacking applicable provenance or safety support shall fail closed at the Astra-governed promotion boundary. |
| NFR-017 | Learning Provenance | Must | A promoted Learning Artifact shall be traceable to its available origin, evidence, version, and promotion decision. |
| NFR-018 | Learning Reversibility | Must | Promoted Memory and Skills shall be disableable or rollback-capable without deleting their historical provenance, evidence, or decisions. |
| NFR-019 | Learning Isolation | Must | Learning state shall remain isolated by the declared execution profile and Hermes Home, and shall not be silently shared outside its applicable scope. |
| NFR-020 | Learning Non-Interference | Must | Learning and Curator maintenance shall not weaken Task governance or prevent eligible Task work from taking priority. |

## 11. Product Metrics and Success Standards

Metrics define what the product measures. Quantitative release thresholds belong
to the future `ACCEPTANCE.RELEASE` Authority and must not be invented here by
implementation or tests.

| ID | Category | Metric | Product definition |
|---|---|---|---|
| METRIC-001 | Outcome | Task success rate | Proportion of evaluated Tasks whose required business outcome is independently supported by acceptable evidence. |
| METRIC-002 | Correctness | False completion rate | Proportion of Tasks recognized as complete when required business outcomes are absent or inconsistent. |
| METRIC-003 | Safety | Duplicate effect rate | Frequency of unintended repeated external effects for the same product intent. |
| METRIC-004 | Safety | Authorization violation count | Controlled effects performed without applicable scope or approval. |
| METRIC-005 | Cancellation | Post-cancel effect count | New controlled effects initiated after cancellation became authoritative. |
| METRIC-006 | Recovery | Recovery success rate | Proportion of interrupted Tasks that reach a correct, explainable continuation, reconciliation, or terminal outcome. |
| METRIC-007 | Interaction | Human intervention rate | Proportion of Tasks requiring user information, approval, or operator intervention. |
| METRIC-008 | Efficiency | Task latency | Time from Task acceptance to its current terminal or durable waiting outcome. |
| METRIC-009 | Efficiency | Agent and tool usage | Agent steps, tool calls, and model usage required per evaluated Task outcome. |
| METRIC-010 | Auditability | Evidence completeness rate | Proportion of formal Task outcomes whose required claims can be traced to applicable evidence and decisions. |
| METRIC-011 | Reliability | Unresolved task rate | Proportion of Tasks left without an eligible next action, durable waiting reason, reconciliation need, or terminal explanation. |
| METRIC-012 | Evaluation | Comparative reliability delta | Difference between defined system configurations for the same metric under comparable product conditions. |
| METRIC-013 | Adaptive Learning | Evidence-supported Learning Artifact rate | Proportion of evaluated Learning Artifacts whose declared provenance and applicable outcome and safety evidence are available for a governed decision. |
| METRIC-014 | Adaptive Learning | Skill reuse rate | Proportion of applicable evaluated Tasks in which a promoted Skill is observably reused. |
| METRIC-015 | Adaptive Learning | Skill outcome delta | Difference in independently observed Task outcome and efficiency metrics between comparable Skill-enabled and control conditions. |
| METRIC-016 | Adaptive Learning | Unsafe learning rate | Proportion of evaluated Learning Artifacts or reuse events associated with false success, unsafe behavior, sensitive transfer, incompatible scope, or authority expansion. |
| METRIC-017 | Adaptive Learning | Rollback effectiveness | Proportion of evaluated rollback cases in which the affected Learning Artifact no longer influences applicable future execution while its history remains auditable. |

The product is successful when:

- the capabilities required by the intended release have registered downstream
  Design and Acceptance coverage;
- evaluated Task outcomes can be distinguished from Agent self-reporting;
- safety and correctness failures are visible rather than converted into false
  success;
- interruption, approval, cancellation, and reconciliation produce explainable
  product outcomes;
- product claims are supported by evidence appropriate to their scope;
- unresolved thresholds and design gaps remain explicit until their Authority
  is established.

## 12. Product Constraints, Assumptions, and Risks

### 12.1 Constraints and assumptions

- Hermes remains the Agent execution capability and exposes a sufficiently
  stable integration boundary.
- Initial business validation may use controlled mock services, but product
  meaning must remain applicable to replaceable business integrations.
- Initial deployment and concurrency scope may be narrower than a distributed
  multi-node product.
- Provider credentials and live external environments may not always be
  available; absence of live validation must remain visible in product claims.
- Reliability evaluation requires versioned tasks, conditions, and metric
  definitions before results can be compared credibly.

### 12.2 Product risks

| Risk | Product consequence | Required treatment |
|---|---|---|
| Hermes integration behavior changes | Agent execution observations or controls may no longer mean what Astra expects. | Record the compatibility impact and update the owning Architecture or Design Topic before claiming support. |
| External systems provide weak idempotency or ambiguous results | Astra may be unable to prove whether an effect occurred. | Preserve uncertainty and require reconciliation rather than guessing. |
| Evaluation uses system-generated success claims as truth | Reliability results become circular and misleading. | Keep evaluation outcome evidence independent of the evaluated success decision. |
| Product and implementation terminology diverge | AI and developers may implement different meanings for the same requirement. | Use this terminology and registered Topic Authorities as the only normative vocabulary. |
| Release thresholds remain undefined | Production readiness cannot be judged consistently. | Keep release status unclaimed until `ACCEPTANCE.RELEASE` is registered and frozen. |
| Future Workflow, Browser, Provider, Recovery, or multi-node scope overlaps current Topics | Parallel Authorities or hidden product expansion may emerge. | Change `PRODUCT.CORE` first, then register or amend the applicable Architecture, Design, and Acceptance Topics. |

### 12.3 Open product governance gaps

- Quantitative production reliability thresholds are not yet defined.
- Production / Release Acceptance has no Frozen Authority.
- Formal comparative evaluation requires a registered Evaluation Method Design
  and Engineering Acceptance Topic before execution.
- Workflow Runtime, Browser Runtime, Provider Framework, and multi-node Runtime
  are not current product commitments.

## 13. Product Traceability

This matrix routes product requirements to their downstream Topic Authorities.
It does not copy Architecture, Design, or Acceptance content.

| Requirement IDs | Primary downstream Topics | Current acceptance routing |
|---|---|---|
| GOAL-001, FR-003–FR-005 | ARCH.HERMES_ASTRA_BOUNDARY | Engineering Acceptance to be registered for the applicable capability scope. |
| GOAL-002, FR-001–FR-002, FR-012–FR-014, FR-022–FR-030 | DESIGN.GOVERNANCE.LIFECYCLE, DESIGN.RUNTIME.DURABLE_TASK | ACCEPTANCE.ENGINEERING.P4 where within P4 scope. |
| GOAL-003, FR-006–FR-011, FR-023–FR-024 | DESIGN.GOVERNANCE.TASK_CONTRACT, DESIGN.GOVERNANCE.EFFECT_IDENTITY, DESIGN.GOVERNANCE.APPROVAL, DESIGN.RUNTIME.DURABLE_TASK | ACCEPTANCE.ENGINEERING.P4 where within P4 scope. |
| GOAL-004, FR-015–FR-019 | DESIGN.GOVERNANCE.EVIDENCE, DESIGN.GOVERNANCE.COMPLETION, DESIGN.GOVERNANCE.DECISIONS | ACCEPTANCE.ENGINEERING.P4 where within P4 scope. |
| GOAL-005, FR-020–FR-030 | DESIGN.GOVERNANCE.RULES, DESIGN.GOVERNANCE.POLICY, DESIGN.GOVERNANCE.DECISIONS, DESIGN.RUNTIME.DURABLE_TASK | ACCEPTANCE.ENGINEERING.P4 where within P4 scope. |
| GOAL-006, FR-031–FR-033 | DESIGN.GOVERNANCE.EVIDENCE, DESIGN.RUNTIME.DURABLE_TASK | Engineering Acceptance to be registered for any scope not covered by P4. |
| GOAL-007, FR-035–FR-042, METRIC-001–METRIC-012 | Future Evaluation Method and Engineering Acceptance Topics | Not yet registered; remains a declared governance gap. |
| GOAL-008, FR-033–FR-034 | Future Operator Experience / Frontend Design Topic — not yet registered | Engineering Acceptance to be registered with the demonstration capability. |
| FR-043–FR-044, NFR-001–NFR-015 | Applicable Architecture and Design Topics | ACCEPTANCE.RELEASE is required for production/release claims and is not yet active. |
| GOAL-009, FR-045–FR-056, NFR-016–NFR-020, METRIC-013–METRIC-017 | Future Evidence-Governed Learning Architecture and Design Topics | Not yet registered; remains an explicit governance gap until the learning capability receives downstream Design and Engineering Acceptance authorities. |

## 14. Requirement Change Policy

- Requirement IDs are stable and must not be reused for different meanings.
- Editorial clarification may retain the same ID when it does not change product
  behavior, scope, obligation, or success meaning.
- A changed product obligation receives a new Requirement ID; the prior
  requirement remains traceable as superseded.
- Adding Workflow, Browser, Provider Framework, multi-node Runtime, or another
  new product domain requires an explicit `PRODUCT.CORE` change before creating
  downstream Architecture or Design authority.
- Current implementation status belongs to `docs/PHASE_STATUS.md`, not this PRD.
- Engineering and release decisions must cite Requirement IDs and the applicable
  Topic Authorities rather than reproducing requirement text.

## 15. Change Log

| Version | Effective date | Status | Change |
|---|---|---|---|
| `1.0` | `2026-07-23` | Superseded by `1.1` | Established the Reliability & Governance Layer product scope: governance, evidence, durable Task Runtime, evaluation, and release-governance requirements around Hermes Agent Runtime. |
| `1.1` | `2026-08-05` | FROZEN | Minimally extended `PRODUCT.CORE` with evidence-governed adaptive learning integration: bounded Hermes Memory and Skill integration, Learning Artifact provenance, evaluation, governed promotion decisions, audit, and rollback. It did not add Continuous Evolution requirements, Workflow Pattern, a Workflow Engine, a Memory platform, or an Astra learning engine. Continuous Evolution remains outside the current Product Definition and will require a future `PRODUCT.CORE` revision. |
| `1.2` | `2026-08-06` | FROZEN | Refactors product positioning and document organization without changing Product Scope, Requirement IDs, or requirement meaning. Clarifies Hermes as the Agent Runtime and Astra as the Reliability & Governance Layer, establishes their product boundary, and groups existing requirements by responsibility domain. |
| `1.3` | `2026-08-06` | FROZEN | Final product-definition refinement: retains Product Boundary as the single complete responsibility definition, removes repeated capability lists and architecture decision rules, and preserves Product Scope, requirements, success meanings, traceability, and governance authority. |
