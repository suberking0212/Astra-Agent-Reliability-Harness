# Astra Current Implementation Status

> **Documentation Governance**
> - **Role:** The repository's sole current implementation-status register.
> - **Authority:** Informational status — non-normative.
> - **Scope:** Current implementation, validation limits, blockers, and next work.
> - **Not Responsible For:** Product requirements, architecture decisions, Runtime Design, acceptance criteria, or release approval.
> - **Depends On:** GOVERNANCE.DOCUMENTATION
> - **Status:** CURRENT; the only current implementation-status source of truth.

This document reports the current worktree and audit evidence. `PASS` means the
named slice has implementation and relevant evidence; it is not a release
approval. Historical test counts and scripted-provider reports are not the
current baseline.

## Current Status

| Area | Status | Current implementation and evidence | Limit / missing work |
| --- | --- | --- | --- |
| Phase 3/4 Runtime and Governance | PARTIAL | Production Runtime, Governance, Gateway, durable Task/Attempt/Execution, approval, cancellation, evidence, receipts, reconciliation, and recovery paths exist. Frozen semantics remain in `docs/phase3/` and `docs/phase4/`. | The current cross-process restart test fails on a `dynamic_authority` / `phase3.governance` import cycle. Full-suite success is not claimed. |
| Hermes CLI integration (0.18.2) | PARTIAL | `scripts/start_astra_cli.sh`, the project plugin, Runtime service, and launcher composition exist. Hermes remains the sole conversation loop. | No live external-provider validation. Current semantic CLI E2E is not a passing baseline because the audit observed a hanging Hermes subprocess. |
| Parent-session binding and toolset projection | PARTIAL | The launcher attaches `astra_capabilities` through Hermes' invocation-level toolset and verifies the projected schema before entering the parent CLI session. | It is a bounded 0.18.2 integration, not a public Task-to-session execution API. |
| Semantic Read Broker | PARTIAL | The only model-visible Astra capability is `astra_get_latest_customer_order`; it creates governed internal work and projects a business result with receipt/evidence references. | One read capability only; its end-to-end CLI claim remains limited to local scripted-provider fixtures. |
| Stage 2A replacement: session correlation | PARTIAL | Astra stores session-to-Task correlation after semantic capability work starts and fails closed on conflict. | Legacy `astra_submit_task` correlation tests no longer match the registered model surface and currently fail; native pause/resume, input/approval handoff, and task context injection remain unavailable. |
| Learning Foundation | PARTIAL | Astra persists content-free Artifact Action Observation records and can correlate session, Task, Attempt, and Execution when a binding exists. | It does not provide native Artifact Used Observation, reuse, applied state, stable Artifact version, native provenance, Memory snapshot inclusion, effectiveness, promotion, rollback, or Artifact lifecycle governance. |
| Stage 3A-0 public-interface feasibility | PASS | The command API spike established that public command handlers receive raw arguments and cannot implicitly resume an agent turn; ambiguous session binding fails closed. | The result is a NO-GO for implicit agent resume, not a delivery of native continuation. |
| Stage 3A-1 Artifact Action Observation | PARTIAL | Public `post_tool_call` telemetry is parsed into content-free, idempotent persisted observations and correlated through the Astra store. | It proves only `post_tool_call -> Artifact Action Observation -> correlation -> persisted evidence`; it is not a native Artifact Used Observation. |
| Hermes 0.20.0 integration | BLOCKED | The repository remains pinned to Hermes 0.18.2. | No immutable 0.20.0 upstream release identity was available for the recorded assessment; no 0.20.0 compatibility claim exists. |
| Memory, Skill, Curator, and learning Layers 1-3 | BLOCKED | ADR-005 keeps ownership with Hermes and permits only bounded Astra evidence association. | Hermes lacks the required public Task-bound execution input, actual native Artifact reference/version, and provenance interfaces. |

## Evidence Boundaries

- Current implementation is supported by focused unit and integration tests in
  `tests/`, but the full suite is not green: the known restart import-cycle
  failure and a Hermes CLI E2E hang remain blockers.
- `artifacts/evaluation/` and records under `docs/history/` retain historical,
  scripted-provider evidence only. They do not prove live-model autonomy,
  learning effectiveness, or release readiness.
- The Documentation Governance checker is a separate repository-documentation
  gate; its pass result does not validate Runtime behavior.

## Current Blockers

1. Repair the Phase 4 cross-process import cycle before making durable-restart claims.
2. Stabilize and time-bound the semantic Hermes CLI E2E before treating it as current integration evidence.
3. Replace retired Stage 2A `astra_submit_task` tests with tests of the semantic-capability correlation contract.
4. Wait for a verifiable Hermes public interface before beginning native learning Artifact integration or a 0.20.0 migration.

## Next Work

1. Restore a green Runtime test baseline, including restart and CLI E2E paths.
2. Define and test the semantic session-correlation contract without exposing Runtime mechanics to the model.
3. Keep Stage 3A-1 as observation-only until Hermes provides native Artifact Used Observation, version, and provenance interfaces.

## Navigation

- Frozen ownership and Runtime contracts: [Documentation Authority](AUTHORITY.md).
- Architecture decisions: [`docs/adr/`](adr/).
- Historical phase plans, legacy protocol descriptions, and version research:
  [`docs/history/`](history/).
