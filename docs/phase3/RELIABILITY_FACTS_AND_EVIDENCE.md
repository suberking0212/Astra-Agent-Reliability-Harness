# Phase 3 Reliability Facts and Evidence Contract

> **Documentation Governance**
> - **Role:** Normative evidence and record-authority specification.
> - **Authority:** A3 — Runtime Design / Facts and Evidence.
> - **Topic:** DESIGN.GOVERNANCE.EVIDENCE
> - **Scope:** Authoritative records, Reliability Facts, Outbox, ExternalOperation evidence, Evidence Collection, and EvidenceSnapshot.
> - **Not Responsible For:** Task state transitions, policy actions, evaluator-specific logic, scheduling, or general observability UX.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.HERMES_ASTRA_BOUNDARY, DESIGN.GOVERNANCE.LIFECYCLE, DESIGN.GOVERNANCE.EFFECT_IDENTITY
> - **Status:** FROZEN

> 状态：`FROZEN / 2026-07-20`  
> 目标：定义 Authoritative Record、Reliability Fact、Outbox、External Operation、Evidence Collection 和 EvidenceSnapshot 的关系，避免第二真相源。

## 1. Authority model

当前真相必须来自权威记录或其固定、带版本的 Snapshot：

```text
Task / Attempt / Execution row
Receipt
Interaction
ExternalOperation
External Business Entity
```

Reliability Fact 是不可变的治理证据索引，只证明某个来源在某个时间观察或确认了什么。Fact 不替代权威记录，也不得通过事件回放推断当前 Task 状态。

Policy、Rule 和 completion gate 的正式输入必须是：

```text
authoritative_snapshot
+ supporting_fact_window
```

如果 Task row 与历史 `task_state_changed` Fact 冲突，当前状态以带 version 的 Task row 为准，冲突本身进入 reconciliation 或审计。

## 2. Fact envelope

```json
{
  "schema_version": "1",
  "fact_id": "uuid",
  "fact_type": "receipt_recorded",
  "task_id": "task-1",
  "attempt_id": "attempt-1",
  "execution_id": "execution-1",
  "source": {
    "kind": "receipt_store",
    "name": "astra",
    "version": "1"
  },
  "authority_scope": "receipt",
  "source_event_id": "receipt-123-created",
  "subject_ref": {
    "type": "execution_receipt",
    "id": "receipt-123",
    "version": 1
  },
  "occurred_at": "2026-07-19T10:00:00Z",
  "recorded_at": "2026-07-19T10:00:01Z",
  "causation_fact_id": null,
  "evidence_refs": ["receipt-123"],
  "attributes": {
    "receipt_hash": "sha256:...",
    "tool_name": "create_complaint_ticket"
  }
}
```

`authority_scope` 由 Astra source registry 按来源赋值，不能由外部生产者自由声明。

## 3. Core fact types

第一版核心类型保持很小：

```text
attempt_started
attempt_ended
execution_started
execution_ended
tool_invocation_started
tool_invocation_finished
side_effect_requested
side_effect_acknowledged
external_operation_confirmed
receipt_recorded
business_state_observed
interaction_opened
interaction_resolved
result_submitted
task_state_changed
```

`side_effect_committed` 不作为普通 Hermes/Tool Result 可直接产生的核心事实。需要强完成证据时使用 `external_operation_confirmed` 或带版本的 `business_state_observed`。

已知核心类型校验必要 attributes，同时保留未知 attributes。扩展类型使用 namespaced string，例如 `vendor.adapter.custom_observation`；未知扩展 Fact 可以持久化，但在注册 Rule 前不得影响 Policy。

Hermes 特有错误码、Provider 信息和 Adapter 元数据只能放在 attributes。核心 Rule 和 Policy 不依赖固定错误字符串。

## 4. Authority matrix

| 来源 | 可以权威证明 | 不能证明 |
| --- | --- | --- |
| Hermes Adapter | Hermes 做了什么、返回什么、Loop 为什么停止 | 业务副作用真实提交、Task 成功 |
| Tool Gateway | 调用是否进入 Gateway、是否被允许、返回内容 | 外部业务当前状态 |
| Receipt Store | Receipt 是否持久化 | 当前业务对象仍然存在或最终一致 |
| Business Adapter | 远端响应、远端 operation ID、业务状态观察 | Task 状态 |
| Task Runtime | Task/Attempt/Execution 状态 | 外部业务提交结果 |
| Interaction Store | Interaction pending/resolved 状态 | Completion Contract 满足 |

## 5. Local atomic publication

对于 Astra 控制的同一数据库，必须在一个事务内提交：

```text
authoritative record mutation
+ reliability_fact
+ optional outbox row
```

Fact 已持久化与 Fact 已投递必须分离。`delivery_status`、`delivery_attempts` 和 next retry time 属于 Outbox，不属于 Fact 业务语义。

本地唯一约束至少覆盖：

```text
fact_id
(source.kind, source.name, source_event_id)
```

Fact append-only；更正通过新 Fact 或新的权威 record version 表达，不修改历史 Fact。

## 6. External operation ledger

外部业务系统不能与 Astra 数据库使用普通原子事务。所有有副作用的外部调用必须先建立本地 `ExternalOperation`：

```text
operation_id
task_id / attempt_id / execution_id
task_contract_ref
authority_domain
effect_identity
effect_request_hash
effect_type / effect_type_version
subject_ref
idempotency_key
approval_ref, optional
external_operation_id
status
version
created_at / acknowledged_at / confirmed_at
```

`effect_identity`、`effect_request_hash` 和 `idempotency_key` 的关系由 `EFFECT_IDENTITY_AND_IDEMPOTENCY.md` 规范。ExternalOperation 不得根据 Tool 名称、Tool call ID、Execution ID 或模型提供的任意 idempotency string 自行定义副作用身份。

第一版必须保证：

```text
UNIQUE(authority_domain, effect_identity)
```

需要 Approval 的 effect 必须先通过 `APPROVAL_BINDING.md` 的 exact identity match，并在同一本地事务中消费/保留 Approval usage 与创建或复用 `prepared` ExternalOperation。

状态：

```text
prepared
in_flight
acknowledged
confirmed
indeterminate
failed
```

流程：

```text
persist prepared
→ call external system with idempotency key
→ persist acknowledged response
→ collect authoritative remote state
→ confirmed | failed | indeterminate
```

远端成功后本地崩溃时，重启后必须先按 effect identity 复用已有 ExternalOperation，再根据 idempotency key、external operation ID 或业务状态重新对账。没有幂等和查询能力时，只能记录 `indeterminate`，不得声称 exactly-once。

## 7. Evidence Collection

Evidence Collector 负责读取权威记录并持久化观察：

- Receipt；
- Interaction；
- ExternalOperation；
- External Business State；
- 必要的 Task/Attempt/Execution 版本。

Collector 可以执行只读查询和 reconciliation，不得修改业务状态、触发补偿 Tool、判断 Requirement 或修改 Task。

外部读取产生 `business_state_observed`，至少保留：

```text
external_operation_id
business_version
observed_at
observation_confidence
state_hash / normalized state summary
```

## 8. EvidenceSnapshot

Evidence Collection 完成后生成不可变 EvidenceSnapshot：

```text
evidence_snapshot_id
task_id / attempt_id
task_version / attempt_version
task_contract id / version / hash
receipt refs + content hashes
interaction refs + versions
external operation refs + versions
effect identity / request hash refs
approval request/resolution refs + versions, if required
business observation refs
fact_watermark
collector_version
created_at
content_hash
```

`fact_watermark` 必须使用本地单调 sequence，不能只依赖时间戳。Evaluator、Rule 和 Policy 不得在一次决策中混用 Snapshot 之外的后到状态。

相同 collection trigger 和相同权威版本应复用同一个 Snapshot，而不是重复产生内容相同的新 Snapshot。

## 9. Neutral Trace separation

Neutral Trace 用于观测、调试和独立评估，不参与在线控制。Reliability Fact 是小规模、带来源权威的治理输入。Policy 不直接读取原始 Neutral Trace。

同一动作可以同时产生 Trace span 和 Fact，但两者拥有不同契约、存储语义和消费者。
