# Astra Agent Reliability Harness

Phase 2 implements the first contract-based Astra × Hermes business vertical
slice without modifying the Hermes Agent Loop.

Current status:

```text
Phase 2 Core: CLOSED AND BASELINED
Phase 2 Live Validation: PENDING (missing explicit credentials)
Phase 3 Design: FROZEN
Phase 3 Contract Domain Layer: IMPLEMENTED
Phase 3 Runtime Governance Core: IMPLEMENTED AS A PHASE 2 RUNTIME COMPONENT
Phase 3 Acceptance: COMPLETE AND FROZEN
```

The Phase 3 ownership boundary is:

> Hermes owns Agent execution reliability. Astra owns Task truth and governance.

Hermes runs the Agent Loop inside an Astra Execution: it interprets context,
selects and orders tools, adapts to tool results, performs local replanning, and
controls Agent-loop-level progress. Its loop guards are limited to preventing
repeated calls, local lack of progress, and runaway loops inside one Agent
Loop. Astra owns Task-level progress across Executions and Attempts, including
persistent lifecycle state, execution constraints, authoritative evidence,
completion evaluation, policy, and runtime transitions.

Hermes decides how to execute. Astra decides whether an action is allowed,
whether the Task Contract is satisfied, and which facts may be formally
recognized.

Phase 3 contracts are frozen after two rounds of scenario validation. The
business-agnostic contract domain layer is implemented as immutable models,
value objects, registries, and pure domain services under `astra/phase3/`.
The minimal Runtime Governance Core is composed into the existing
`Phase2Runtime`; no parallel Runtime, workflow engine, coordinator, dispatcher,
or orchestrator has been added.

The reproducible completion baseline is archived under
`artifacts/phase3-governance-complete/` and identified by Git tag
`phase3-governance-complete`. From this baseline onward, Phase 3 implementation
is frozen. Runtime or contract changes require explicit Phase 4 acceptance
criteria; code cleanup, architectural preference, or opportunistic optimization
is not sufficient justification to modify the frozen implementation.

## Phase 3 CLI development entry

Phase 3 uses a CLI as its only development entry. It reads persisted contract
JSON or the frozen table-driven fixture and delegates all decisions to the
pure domain layer:

```bash
python -m astra.phase3 validate-round2 \
  --fixture tests/fixtures/phase3_contract_round2.json

python -m astra.phase3 validate-governance-round2 \
  --fixture tests/fixtures/phase3_contract_round2.json

python -m astra.phase3 validate-contract /path/to/materialized-contract.json
```

After installing the project, the equivalent command is `astra-phase3`.
Web APIs and WebUI are intentionally outside the Phase 3 scope.

The implemented Phase 2 execution path is:

```text
RuntimeInvocation
→ HermesExecutor
→ Hermes Agent Loop
→ fixed astra_bridge project plugin
→ Astra Tool Gateway
→ Business Adapter / SQLite-backed MockBusinessService
→ submit_task_result
→ ResultReceipt
→ ExecutionResult
→ NeutralTraceCollector
```

The bridge plugin only exposes controlled tool schemas, forwards calls to the
gateway, injects persisted runtime feedback, and emits Hermes execution
observations. The gateway and runtime remain responsible for permissions,
approval, idempotency, receipts, and confirmation of external side effects.
Observer hooks show what Hermes observed; they do not by themselves prove an
external business operation or authoritative Task state.

In the Phase 3 design, `submit_task_result` is a result submission, not a Task
completion decision. The target decision path is:

```text
persist result submission
→ collect or fix an EvidenceSnapshot
→ evaluate the Completion Contract
→ apply Task Policy
→ Runtime completion gate
→ CAS transition
```

When input or approval is required, Astra persists the Interaction, terminates
the current Execution, and moves the Task and Attempt into a waiting state.
After resolution, the runtime creates a new Astra Execution and starts a new
Hermes turn, reusing or rebuilding the Hermes Session according to session
policy. It never resumes a saved Python call stack.

## Run the deterministic test suite

```bash
pytest -q
```

The normal complaint E2E test uses a scripted provider boundary while keeping
the real Hermes plugin discovery, tool registry, Agent Loop, tool execution,
observer hooks, and SessionDB persistence.

## Opt in to the live Provider smoke test

The live test is skipped by default and never blocks ordinary CI. It is an
**environment-dependent non-blocking test**: external Provider credentials and
network availability are required, it is not part of the Phase 3 core Gate,
and its skip does not affect Phase 3 acceptance.

```bash
export ASTRA_RUN_LIVE_PROVIDER=1
export ASTRA_LIVE_BASE_URL="https://provider.example/v1"
export ASTRA_LIVE_API_KEY="..."
export ASTRA_LIVE_MODEL="model-name"
# Optional: ASTRA_LIVE_PROVIDER and ASTRA_LIVE_API_MODE
pytest -q tests/test_phase2_live_provider.py
```

Business side effects remain confined to the local Mock Business Service.

## Reproduce the Phase 2 Core audit and baseline

```bash
python3 scripts/run_phase2_core_audit.py
pytest -q tests/test_phase2_stress.py
python3 scripts/freeze_phase2_core_baseline.py
```

Generated evidence is stored under `artifacts/phase2-core-runtime-audit/`.
The frozen source archive and manifest are written to
`artifacts/phase2-core-baseline-source.tar.gz` and
`artifacts/phase2-core-baseline-manifest.json`.

## Package boundaries

- `astra/domain.py`: Hermes-independent executor, event, result, and tool ports.
- `astra/hermes_adapter/`: the only layer allowed to import Hermes.
- `astra/tool_gateway.py`: whitelist, schema, permission, suspension, receipt,
  and idempotency enforcement.
- `astra/result_validator.py`: minimal contract/business-state validation.
- `astra/storage.py`: SQLite persistence and exactly-once termination.
- `astra/runtime.py`: persisted interaction resolution, new Execution creation,
  and new Hermes-turn resume.
- `astra/budget.py`: observe-only/conservative usage ledger.
- `astra/trace.py`: neutral, non-intervening trace collection.
- `astra/phase3/task_contract.py`: strict Task Contract v1 envelope, content
  hashing, authorization references, and workflow-field rejection.
- `astra/phase3/effects.py` and `approval.py`: canonical effect identity,
  versioned normalizer registry, idempotency keys, ExternalOperation value
  objects, and exact approval binding.
- `astra/phase3/completion.py` and `policy.py`: immutable EvidenceSnapshot,
  evaluator registry, generic completion aggregation, PolicyDecision, and pure
  CAS/completion-gate checks.
- `astra/phase3/governance.py`: callable Runtime Governance Core for exact
  approval consumption, ExternalOperation identity, authoritative Task/Attempt
  CAS, and atomic Reliability Fact/Outbox publication. It reuses the existing
  Phase 2 store/transaction and is invoked by `Phase2Runtime`.
- `astra/phase3/round2.py`: pure fixture-facing contract-chain validation; it
  does not select tools, execute business actions, or coordinate workflows.
- `astra/phase3/cli.py`: thin, machine-readable development adapter for the
  Phase 3 domain layer; no Web API or WebUI surface is included.

## Phase 3 design contracts

- `docs/adr/ADR-003-task-reliability-boundary.md`: Hermes/Astra ownership and
  non-goals.
- `docs/phase3/TASK_CONTRACT.md`: normalized task objective, subject, scope,
  capability, constraint, effect authorization, approval, and completion refs.
- `docs/phase3/EFFECT_IDENTITY_AND_IDEMPOTENCY.md`: canonical effect request,
  effect identity, ExternalOperation, idempotency, and reconciliation identity.
- `docs/phase3/APPROVAL_BINDING.md`: exact effect-bound approval requests,
  resolutions, credentials, validity, and consumption semantics.
- `docs/phase3/TASK_LIFECYCLE.md`: Task, Attempt, Execution, Interaction, and
  bounded lifecycle semantics.
- `docs/phase3/RELIABILITY_FACTS_AND_EVIDENCE.md`: authoritative records,
  facts, outbox, external operations, evidence collection, and snapshots.
- `docs/phase3/TASK_RULES.md`: deterministic task-level rule contract.
- `docs/phase3/TASK_POLICY.md`: task-level actions and advisory feedback.
- `docs/phase3/REQUIREMENT_EVALUATORS.md`: requirement plugins and completion
  aggregation.
- `docs/phase3/DECISION_POINTS_AND_SNAPSHOTS.md`: consistent decision inputs,
  idempotency, CAS, and atomic application.
- `docs/phase3/CONTRACT_SCENARIO_VALIDATION_ROUND2.md`: seven-scenario,
  five-path contract fixtures and the Phase 3 design freeze evidence.

Reliability Facts are governance evidence, not a second source of truth.
Current state remains authoritative in versioned Task/Attempt/Execution,
Receipt, Interaction, ExternalOperation, and business-system records or their
immutable evidence snapshots.
