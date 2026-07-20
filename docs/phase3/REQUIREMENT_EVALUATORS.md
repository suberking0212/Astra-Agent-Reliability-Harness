# Phase 3 Requirement Evaluator Contract

> 状态：`FROZEN / 2026-07-20`  
> 旧业务耦合 Outcome Validator 被拆分为 CompletionContract、Requirement Evaluator Registry、RequirementEvaluation 和 Completion Aggregator。

## 1. Completion structure

```text
CompletionContract
→ CompletionRequirement[]
→ EvidenceSnapshot
→ Evaluator Registry
→ RequirementEvaluation[]
→ Completion Aggregator
→ CompletionValidationResult
```

Agent 只提交 outcome、evidence refs 和 receipt refs，不能提交或修改 Completion Contract。

## 2. CompletionContract

第一版字段：

```text
contract_id
contract_version
task_type
requirements[]
aggregation_policy = all_required
```

第一版只支持 `all_required`，不建设任意布尔表达式、Requirement DSL 或 Workflow。

## 3. CompletionRequirement

```json
{
  "requirement_id": "complaint_ticket_created",
  "description": "A complaint ticket exists for the correct customer and order.",
  "required": true,
  "evaluator": {
    "evaluator_id": "complaint.ticket_exists_and_matches",
    "evaluator_version": "1"
  },
  "configuration": {
    "submitted_ticket_field": "ticket_id"
  },
  "required_evidence": ["execution_receipt", "business_state"]
}
```

Core 将 `configuration` 视为 evaluator-owned opaque mapping，不发展通用表达式语言。Completion Contract 必须固定 evaluator version，防止插件升级改变旧 Task 的验收语义。

## 4. Evidence collection before evaluation

Evaluator 不能边读取业务系统边写 Fact。正式流程：

```text
Evidence Collector
→ persist authoritative observations
→ immutable EvidenceSnapshot
→ pure Requirement Evaluator
```

Evaluator 输入：

```text
CompletionRequirement
SubmittedResult
EvidenceSnapshot
referenced Receipt snapshots
```

对副作用 Requirement，EvidenceSnapshot 应携带 Task Contract ref、effect identity/request hash、ExternalOperation 和必要 Approval refs。Evaluator 可以验证业务特定的批准与结果语义，但不得重新规范化 raw Tool arguments 或生成新的 effect identity。

Evaluator 不获得：

- Task/Attempt 写接口；
- Tool Gateway 执行接口；
- Hermes Agent 或 Hermes 私有类型；
- Policy/Interaction/Reconciliation 创建接口；
- 实时可变 Business Service client。

## 5. Evaluator Registry

Registry key：

```text
(evaluator_id, evaluator_version)
```

第一版使用静态注册。新增 Complaint、Refund 或其他 Evaluator 不修改 Aggregator。未注册或异常 Evaluator 返回明确 `evaluator_error`，不得默认通过。

## 6. RequirementEvaluation

```json
{
  "evaluation_id": "evaluation-1",
  "requirement_id": "complaint_ticket_created",
  "evaluator_id": "complaint.ticket_exists_and_matches",
  "evaluator_version": "1",
  "evidence_snapshot_id": "snapshot-1",
  "status": "satisfied",
  "evidence_refs": ["receipt-123"],
  "observed_fact_refs": ["fact-ticket-observed"],
  "message": "Ticket exists and matches the required customer and order.",
  "details": {}
}
```

状态：

```text
satisfied
unsatisfied
unknown
evaluator_error
```

- `unsatisfied`：已有权威证据证明 Requirement 未满足；
- `unknown`：证据不足、外部状态不可读或 operation 尚未确认；
- `evaluator_error`：插件或配置错误。

相同 requirement、submitted result、EvidenceSnapshot 和 evaluator version 必须产生相同结果并复用同一个 Evaluation。

## 7. Completion Aggregator

第一版聚合优先级：

```text
任何 required evaluator_error → evaluator_error
否则任何 required unsatisfied → unsatisfied
否则任何 required unknown → indeterminate
全部 required satisfied → satisfied
```

输出 `CompletionValidationResult`：

```text
completion_validation_id
contract_id / contract_version
submitted_result_id
evidence_snapshot_id
status
requirement_evaluation_refs
unmet_requirement_ids
unknown_requirement_ids
```

Aggregator 不修改 Task、不创建 Attempt、不生成 Feedback、不调用 Policy，也不理解 Complaint、Ticket、Customer、Order 等业务字段。

## 8. Responsibility separation

- Evaluator 判断单个 Requirement；
- Aggregator 汇总 Requirement 状态；
- Runtime completion gate 检查 pending Interaction、reconciliation、version 和 required ExternalOperation；
- Task Rules 检测跨对象和跨生命周期治理异常；
- Task Policy 决定 Task 级动作。

不再通过 Task Rule 重复包装 `completion_claim_conflict` 或 `false_completion`。这些可以作为离线分析标签，但不进入在线决策链。

## 9. Complaint plugin

Phase 2 `MinimalResultValidator` 中的 Ticket、Customer、Order 和 `create_complaint_ticket` 知识在未来迁移为 Complaint Evaluator。Core Aggregator 不允许出现这些字段或 Tool 名称。

## 10. Contract tests

- Evaluator 不能修改 Task 或调用 Tool；
- Evaluator 重放不写新 Fact；
- Complaint Evaluator 可替换而 Aggregator 不变；
- 未知 evaluator 明确失败；
- evaluator version 被 Contract 固定；
- `unknown` 不会被聚合成 success；
- Hermes 私有类型不穿透 Registry 或 Evaluation；
- Aggregator 不含 Complaint 专有逻辑。
