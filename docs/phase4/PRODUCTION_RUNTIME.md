# Production Runtime

Production is assembled through `astra.production.ProductionRuntime`. One process
creates exactly one `AstraStore` and one `TaskRuntime`; Governance,
`HermesExecutor`, the tool gateway, validation, budget, trace, business adapter,
and `SingleWorker` are all constructed from those same authority objects.

Batch 2 restores the mandatory governance chain for every business Tool call:

```text
authoritative Task/Attempt/Execution
→ frozen Task Contract binding
→ capability + tool version + schema hash + subject/scope + constraints
→ versioned Effect Normalizer, for effect tools
→ persisted CanonicalEffectRequest
→ exact Approval binding, when required
→ prepared ExternalOperation
→ effect-derived idempotency key
→ business authority call
→ authoritative business-state confirmation
```

The Hermes schema does not accept a model-provided production idempotency key.
Unknown handlers or versions, schema drift, ambiguous/absent effect matches,
approval mismatch, and non-running authoritative lifecycle state all deny the
call before a business side effect.

The formal command is installed as `astra-runtime` and is also available as
`python -m astra`.

## Submit a Task

```bash
astra-runtime \
  --database var/astra.sqlite3 \
  submit \
  --command-id submit:example \
  --task-id task:example \
  --contract task-contract.json \
  --completion-contract completion-contract.json
```

## Resolve an Interaction

```bash
astra-runtime \
  --database var/astra.sqlite3 \
  resolve \
  --command-id resolve:example \
  --interaction-id interaction:execution:example \
  --expected-version 1 \
  --resolution-json '{"answer":"confirmed"}'
```

## Run the continuous Worker

```bash
astra-runtime \
  --database var/astra.sqlite3 \
  --hermes-root hermes-agent-main \
  --provider-config '{"provider":"custom","model":"example"}' \
  worker
```

The Worker polls durable pending Run Requests until SIGINT or SIGTERM. Use
`worker --once` for deployment probes.

## Cancel a Task

```bash
astra-runtime \
  --database var/astra.sqlite3 \
  cancel \
  --command-id cancel:example \
  --task-id task:example \
  --expected-task-version 2 \
  --reason operator_cancelled
```

The cancel command first commits one transaction containing the cancel intent,
Task/Attempt cancellation, pending Run Request cancellation, and pending
Interaction cancellation. Only after commit does the production composition
best-effort notify the in-process Executor. A cancelled or otherwise terminal
Task cannot prepare, dispatch, acknowledge, or confirm a new operation. Late
Execution results remain auditable but cannot replace the terminal Task state.

## Inspect Task and business evidence

```bash
astra-runtime \
  --database var/astra.sqlite3 \
  status \
  --task-id task:example \
  --pretty
```

This read-only command reports Task/Attempt/Execution lifecycle state together
with Interactions, exact Approval records, ExternalOperations, Receipts,
authoritative Observations, persisted EvidenceSnapshots, Task Rule evaluations,
ordered Reliability Facts, and linked business objects. The fact sequence makes
the prepared → dispatched → acknowledged → confirmed operation lifecycle
auditable. Operators do not need to edit or query SQLite to resume a Task.

The Batch 1 black-box acceptance test starts this long-lived Worker before task
submission and exercises only the composition-root entry points:

```text
submit
→ worker
→ waiting_input
→ resolve
→ worker
→ succeeded
```

It also verifies that both Executions share one Attempt, the resumed Execution
receives the persisted session and `InteractionResolution` feedback, and the
Worker and Executor reference the composition root's sole `TaskRuntime`.
