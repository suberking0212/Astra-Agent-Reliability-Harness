# Phase 3 Decision Points, Snapshots, Idempotency and CAS

> **Documentation Governance**
> - **Role:** Normative decision-point, snapshot-use, decision-idempotency, and CAS specification.
> - **Authority:** A3 — Runtime Design / Decisions and CAS.
> - **Topic:** DESIGN.GOVERNANCE.DECISIONS
> - **Scope:** Fixed decision triggers, DecisionContext construction, evaluation identity, atomic decision application, and stale-decision handling.
> - **Not Responsible For:** Defining evidence source authority, evaluator semantics, policy choice, or durable worker scheduling.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.HERMES_ASTRA_BOUNDARY, DESIGN.GOVERNANCE.LIFECYCLE, DESIGN.GOVERNANCE.EVIDENCE, DESIGN.GOVERNANCE.COMPLETION
> - **Status:** FROZEN

> 状态：`FROZEN / 2026-07-20`  
> 目标：保证同一触发在并发、崩溃和重启后只形成一个可应用的 Task 决策，并且决策基于一致的固定输入。

## 1. Fixed decision points

Phase 3 第一版只在以下边界求值：

```text
execution_ended
completion_validated
interaction_changed
reconciliation_completed
attempt_limit_reached
```

Task cancel 是 Runtime Command，不要求先经过 Policy。普通 Tool 回调和 Provider error 不直接触发 Task Policy；它们先由 Hermes 处理，必要信息通过 Adapter/Gateway 进入权威记录或 Fact。

## 2. Snapshot construction

一次 DecisionContext 必须绑定固定版本：

- Task row/version；
- Task Contract ID/version/hash；
- Attempt row/version；
- 相关 Execution terminal records；
- Receipt IDs 和 content hashes；
- Interaction IDs、versions 和状态；
- ExternalOperation IDs、versions 和状态；
- effect identity/request hash 和 Approval Request/Resolution versions（如适用）；
- EvidenceSnapshot ID；
- Fact watermark；
- CompletionValidation ID（如果存在）。

本地记录应在一个数据库一致性读取事务内采集。外部状态先由 Evidence Collection 固定为 Observation 和 EvidenceSnapshot，随后 Evaluator、Rule 与 Policy 只读取该 Snapshot。

## 3. DecisionContext

```json
{
  "decision_context_id": "context-1",
  "decision_point": "completion_validated",
  "trigger_id": "validation-1",
  "task_id": "task-1",
  "task_version": 12,
  "task_contract_ref": {
    "contract_id": "contract-1",
    "contract_version": "1",
    "contract_hash": "sha256:..."
  },
  "attempt_id": "attempt-1",
  "attempt_version": 5,
  "evidence_snapshot_id": "snapshot-1",
  "fact_watermark": 183,
  "completion_validation_id": "validation-1",
  "interaction_snapshot_version": 7,
  "policy_id": "astra.default_task_policy",
  "policy_version": "1",
  "context_hash": "sha256:..."
}
```

`context_hash` 必须由规范化、稳定序列化的输入引用和版本生成，不包含非确定性日志文本。

## 4. Evaluation idempotency

建议唯一键：

```text
EvidenceSnapshot:
(task_id, collection_trigger_id, authoritative_versions_hash, collector_version)

RequirementEvaluation:
(requirement_id, evaluator_id, evaluator_version,
 submitted_result_id, evidence_snapshot_id)

RuleEvaluation:
(rule_id, rule_version, decision_point, evidence_snapshot_id,
 task_version, attempt_version)

PolicyDecision:
(task_id, decision_point, trigger_id, context_hash,
 policy_id, policy_version)
```

重复执行必须返回已有对象，而不是创建语义重复记录。

## 5. Decision application

PolicyDecision 只是一项不可变建议。Task Runtime 是唯一状态写入者，应用流程必须在一个本地事务内完成：

```text
BEGIN
  load decision
  verify decision is not already applied
  verify task.version = expected_task_version
  verify attempt.version = expected_attempt_version
  verify referenced Interaction/Completion/Reconciliation state
  apply transition
  create Attempt/Interaction/Reconciliation request if required
  mark decision applied
  insert reliability facts
  insert optional outbox rows
COMMIT
```

CAS 失败表示 Decision 已过期，必须标记为 `stale` 或 `not_applied`，不能对新状态强行重放。

Task Contract version/hash、CanonicalEffectRequest 或 Approval binding 在求值后变化，同样使旧 Decision stale；Runtime 不得只比较 Task row version 而忽略这些绑定引用。

## 6. Derived-object exactly-once

由 PolicyDecision 创建的对象必须记录 `created_by_decision_id`，并建立唯一约束：

```text
attempt.created_by_decision_id
interaction.created_by_decision_id
reconciliation.created_by_decision_id
```

因此同一个 decision 即使在进程重启后再次应用，也不会创建第二个 Attempt、Interaction 或 reconciliation job。

## 7. Completion gate

`complete` 决策只有在当前 Snapshot 同时满足以下条件时才可应用：

```text
CompletionValidationResult.status = satisfied
AND no pending blocking Interaction
AND no unresolved reconciliation
AND no required ExternalOperation in prepared/in_flight/acknowledged/indeterminate
AND Task/Attempt versions match DecisionContext
AND Task is not cancelled or terminal
```

这些是 Runtime completion invariants，不需要为每一项再生成 Task Finding。

## 8. Failure and restart behavior

- Policy 求值后、应用前崩溃：重启后复用同一 DecisionContext 和 PolicyDecision；
- 状态应用事务中崩溃：事务回滚或完整提交，不允许部分状态；
- 外部操作后本地崩溃：由 ExternalOperation reconciliation 处理，不直接重新执行；
- Fact/Outbox 投递失败：权威记录与 Fact 已原子提交，Dispatcher 幂等重投；
- 新状态先于旧 Decision 应用：CAS 拒绝旧 Decision。

## 9. Non-goals

- 不建立全局事件时间顺序；
- 不使用时间戳代替数据库 watermark/version；
- 不要求分布式事务；
- 不通过消息总线协调本地状态转换；
- 不允许 Policy 直接写 Task 状态。
