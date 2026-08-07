# Execution-Type Runtime Implementation Plan

> **Documentation Governance**
> - **Role:** Informational implementation plan.
> - **Authority:** Informational — implementation planning only.
> - **Scope:** Removal of semantic conversation routing and restoration of the frozen Hermes/Astra execution boundary.
> - **Not Responsible For:** Creating product requirements, architecture decisions, Runtime semantics, or acceptance authority.
> - **Depends On:** ARCH.HERMES_ASTRA_BOUNDARY, ARCH.PERSISTENT_TASK_RUNTIME, DESIGN.GOVERNANCE.TASK_CONTRACT, DESIGN.GOVERNANCE.LIFECYCLE, DESIGN.RUNTIME.DURABLE_TASK
> - **Status:** HISTORICAL REFERENCE — superseded by the current CLI and Runtime Design.

## 1. Objective

Remove all Astra-side natural-language routing and restore the frozen
execution boundary:

```text
structured Task Contract
→ Astra RuntimeInvocation
→ Hermes Agent Loop
→ actual tool/runtime/result calls
→ deterministic Astra governance
→ evidence-based completion
```

Astra must not infer task type, business intent, missing fields, tool choice,
capability gaps, or execution complexity from message text.

## 2. Existing execution types and their roles

Keep the existing four `execution_type` values. Their roles must be documented
clearly instead of being used to infer business meaning from natural language:

| Execution type | Role | Consequence |
|---|---|---|
| `direct_response` | Base execution path: a response may complete without a business tool call. | Validate response-oriented completion requirements. |
| `tool_execution` | Base execution path: completion depends on governed tools or authoritative business state. | Require receipts, evidence, or verified business observations. |
| `interaction_required` | Waiting branch reached when required user input is unavailable. | Persist the Interaction and enter `waiting_input`. |
| `approval_required` | Waiting branch reached when a governed effect requires approval. | Persist the approval Interaction and enter `waiting_approval`. |

The first two values are base execution paths. The latter two describe waiting
branches produced by concrete runtime events:

```text
request_user_input / missing required input → waiting_input
request_approval / approval_required       → waiting_approval
```

Resolving either Interaction resumes the original execution path. The existing
field and enum remain unchanged; implementation and documentation must not treat
the four names as four peer business-intent classes.

`execution_type` only identifies the initial execution path. `waiting_input`
and `waiting_approval` are Runtime lifecycle states and are not
`execution_type` values.

## 3. Execution Contract Source

`execution_type` must be supplied as structured input by the Task producer.After Task creation, execution_type is immutable.
Resume, approval, recovery, or replay must not replace it with another execution_type.
Permitted sources include a typed API endpoint, an explicit CLI mode, a
registered product flow that already owns a Task Contract, or a trusted
integration that materializes a versioned Task Contract.

Forbidden sources include keywords, regular expressions, embeddings, or an LLM
router over the message inside Astra Runtime; inferred entities or guessed
missing fields; tool names selected from objective text; and changing
`execution_type` after reading the natural-language objective.

For the interactive CLI, use explicit surfaces instead of invisible routing:

```text
/ask <message>    → direct_response
/task <objective> → tool_execution
```

The business-task surface may remain the default, but the active mode must be
visible and user-selectable.

## 4. Complexity representation

Complexity is not inferred from business-language categories. It is represented
by structured limits and observed execution facts:

```text
TaskContract.limits
provider/model configuration supplied by the caller
actual Agent steps and tool calls
actual Executions and Attempts
actual Interaction and approval events
actual token/cost/deadline consumption
```

The caller may provide different versioned limit profiles, but Astra must not
choose a profile by interpreting the objective. Runtime only enforces persisted
limits.

## 5. Target ingress model

Conversation continuity and Task identity must be separate:

```text
Conversation
├── message-1 → task-1
├── message-2 → task-2
└── message-3 → task-3
```

Each accepted open message receives stable transport identities:

```text
conversation_id
turn_id
message_id
message_hash
task_id
```

This record is identity-only. It must not contain intent, resolution kind,
missing fields, recommended tools, or capability-gap judgments. An idempotent
replay returns the same Task/result; identity reuse with different content
fails with `message_identity_conflict`.

## 6. CLI task lifecycle

Every Execution entry uses the same rule:

```text
Contract
→ Contract Preflight
→ RuntimeInvocation
→ Runtime Execution
```

This applies equally to initial execution, clarification resume, approval
resume, and restart recovery.

### 6.1 New open message

When there is no explicitly selected pending Interaction:

1. Accept the message with a new `message_id`.
2. Select the initial `execution_type` from the visible CLI mode, not message content.
3. Materialize the Task Contract from the selected registered profile.
4. Run `preflight_contract` before persistence.
5. Submit a new Task, Attempt, and Run Request transactionally.
6. Start Hermes with the raw message and the exact resolved tool surface.

### 6.2 Clarification reply

A reply may resume an existing Task only when the CLI has an exact pending
Interaction with `purpose=clarification`, `interaction_id`, `task_id`, `prompt`,
`missing_fields`, and `resume_condition`. Resolving it creates a new Execution
in the same Attempt. It must not mutate the original Contract or input snapshot.

### 6.3 Approval reply

Approval resolution must bind the exact `task_contract_ref`, `effect_identity`,
`effect_request_hash`, `approval_requirement_ref`, and `permission_scope`. A
general yes/no reply may not authorize a different effect.

### 6.4 Normal completion

After a normal final response, clear the active Task. The next open message is
a new Task. Do not create an empty `request_user_input` merely to keep a Task or
Hermes session alive.

## 7. Tool surface

The Task Contract fixes tool identity and access mode:

```text
read
effect
runtime_primitive
result_submission
```

### 7.1 Business read tools

Read tools pass through Tool Gateway capability, version, schema, access-mode,
constraint, and subject-scope checks. They do not require effect intents.

The order domain needs a real collection read tool:

```text
list_customer_orders(customer_id)
access_mode = read
```

This tool, rather than semantic routing, enables listing all orders for a
customer.

### 7.2 Business effect tools

Effect tools are exposed only when the Task Contract contains matching
authorized effects and approval requirements. Open text must never create or
expand effect authority.

### 7.3 Runtime primitives

`request_user_input` and `request_approval` go through the Hermes Adapter to
Astra Runtime. They do not enter Business Sandbox or the ordinary business Tool
Adapter.

### 7.4 Result submission

`submit_task_result` persists a candidate result. It never directly marks the
Task successful. Completion remains:

```text
persist submission
→ collect/fix evidence
→ evaluate Completion Contract
→ apply Task Policy
→ completion gate
→ CAS transition
```

## 8. Tool Catalog integrity

Contract Preflight is the single Contract validation entry for every Execution.
Initial execution and every resumed execution use the same validation flow and
the same immutable Catalog projection for Task Contract bindings, Hermes Bridge
schemas, Tool Gateway specs, and schema/version/access-mode checks. Recovery is
one caller of this shared validation entry, not a separate validation path.

No component may independently recreate `capability_ref`, `tool_version`,
`schema_hash`, or `access_mode`. An incompatible immutable Contract must
terminate with a stable compatibility reason; it must not be silently upgraded
or run until every tool returns `capability_mismatch`.

## 9. Persistence changes

1. Remove the obsolete `conversation_resolutions` table and APIs.
2. Add an identity-only ingress table only when CLI/API idempotency is
   implemented; do not reuse the removed schema.
3. Add explicit Interaction purpose: `clarification` or `approval`.
4. Store conversation identity separately from Task identity.
5. Add a durable compatibility outcome for immutable tool bindings that cannot
   run under the installed catalog.

## 10. Code changes by component

### Interactive CLI

- Remove semantic resolver calls and `execution_type="conversation"`.
- Preserve the existing four `execution_type` values and document their roles.
- Do not introduce a replacement action model or a second routing field.
- Add explicit `/ask` and `/task` modes or equivalent typed entry points.
- Stop assigning `conversation_id = task_id`.
- Resume only the exact selected clarification/approval Interaction.
- Do not print internal reason codes.

### Hermes Executor Adapter

- Remove the continuous-conversation system instruction.
- Remove the empty `request_user_input` convention.
- Inject raw objective, immutable Task Contract, allowed tools, persisted
  feedback, and exact limits.
- Keep tool selection and execution order entirely in Hermes.

### Runtime

- Remove `execution_type == "conversation"` branches.
- Build resumed input only from an exact resolved Interaction.
- Do not modify the immutable Task Contract; construct each new Execution input
  only from the exact resolved Interaction and authoritative persisted records.
- Every new `RuntimeInvocation` must originate from a Contract that has passed
  the same Contract Preflight and Catalog validation.
- Keep restart recovery as a new Execution, never a restored Python stack.

### Tool Gateway and business adapters

- Add `list_customer_orders` end to end.
- Keep Catalog as the single binding source.
- Return structured errors to Hermes; map only stable product messages to UI.

### Storage

- Complete the schema migration removing semantic resolution state.
- Add identity-only ingress and explicit Interaction purpose in later schema
  versions with migration and replay tests.

## 11. Legacy-data handling

Existing Tasks with `execution_type="conversation"` or obsolete capability
bindings must not resume as normal work.

Every legacy `execution_type="conversation"` Contract is non-recoverable. It
must not participate in automatic migration and must not be converted into a
different `execution_type`.

1. Preserve immutable historical Contract JSON for audit.
2. Mark incompatible non-terminal Tasks with a stable reason such as
   `contract_runtime_incompatible`.
3. Cancel pending Run Requests transactionally.
4. Resolve or cancel orphaned generic user-input Interactions.
5. Ask the user to resubmit as a new typed Task.

Do not rewrite historical Contracts in place.

## 12. Acceptance tests

### No semantic routing

- Arbitrary Chinese and English messages follow identical ingress mechanics for
  the same explicit execution mode.
- Core Runtime contains no business-keyword routing.
- Astra never selects a tool from message text.
- Contract Preflight never reads, parses, or interprets natural-language
  message content.

### Execution paths and waiting branches

- `direct_response` can complete with response evidence and no business tool.
  A non-empty assistant message is not completion evidence by itself: the
  Execution must persist a valid `submit_task_result`/`ResultReceipt` through
  the same validation boundary used by other execution types.
- `tool_execution` cannot succeed on Agent text alone.
- A concrete `request_user_input` produces `waiting_input` and resumes through
  a new Execution while preserving the original execution path.
- A concrete approval request produces `waiting_approval` with exact binding.
- No Runtime text-routing logic chooses among the four values from message semantics.

### Conversation/task separation

- Two normal messages in one conversation create two Tasks.
- A clarification reply resumes only its referenced Task.
- Restart does not inject a new message into an unrelated old Task.

### Catalog compatibility

- Contract, Bridge, and Gateway use identical bindings.
- Incompatible persisted bindings fail before Hermes/tool execution.
- No normal run exposes raw `capability_mismatch` to the user.
- Initial execution, clarification resume, approval resume, and restart recovery
  all pass through the same Contract Preflight and the same Catalog validation
  implementation.

### Business behavior

- Hermes can call `list_customer_orders(customer_id)` and list authoritative
  returned orders.
- Tests assert actual Gateway receipts, not keyword-based routing.

## 13. Delivery sequence

### Batch 1 — Remove abandoned design

- Delete ConversationResolution models, resolver, storage, tests, and Draft
  document.
- Remove its Topic and document-registry entries.

### Batch 2 — Clarify the existing execution-type semantics

- Remove `conversation` execution type and special Adapter/Runtime logic.
- Keep `direct_response`, `tool_execution`, `interaction_required`, and
  `approval_required` unchanged.
- Make the first two explicit base paths and the latter two explicit waiting
  branches in code comments, validation, and documentation.
- Add explicit CLI/API execution mode.
- Separate conversation, message, and Task identities.

### Batch 3 — Correct Interaction behavior

- Add explicit clarification/approval purpose.
- Resume only exact pending Interactions.
- Eliminate empty-prompt continuous conversation.

### Batch 4 — Complete business read capability

- Add `list_customer_orders` across Catalog, Gateway, Adapter, Sandbox, and
  tests.

### Batch 5 — Migration and full verification

- Terminate incompatible legacy Tasks safely.
- Run unit, governance, persistent-runtime, Hermes E2E, restart, and live CLI
  acceptance.
- Update walkthrough/status documents only after evidence exists.

## 14. Completion condition

The remediation is complete only when Astra's behavior can be described as:

> The Task producer supplies a structured execution contract. Hermes decides
> how to execute within that contract. Astra governs actual actions and accepts
> only authoritative evidence. No Astra component infers natural-language
> business intent.
