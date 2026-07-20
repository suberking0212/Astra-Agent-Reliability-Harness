# Phase 3 Task Rule Contract

> 状态：`FROZEN / 2026-07-20`  
> 旧 `Failure Detector` 设计被本契约取代。Task Rule 只判断跨对象、跨 Execution/Attempt 和跨重启的治理不变量，不检测 Hermes 应负责的局部执行错误。

## 1. Responsibility

Task Rule 可以检测：

```text
cross_attempt_no_progress
duplicate_side_effect
late_state_affecting_record
recovery_divergence
receipt_business_state_mismatch
```

Task Rule 不检测：

```text
invalid_tool_arguments
tool_timeout
provider retryability
invalid_tool_result parsing
whether to retry the same Tool
whether Hermes should replan
```

后一类属于 Hermes Agent execution reliability；权限、审批、Schema 和幂等 deny 属于确定性 Constraint Boundary。

## 2. Input

```text
TaskRuleContext
├── DecisionContext
├── authoritative Task/Attempt/Execution snapshot
├── EvidenceSnapshot
├── supporting Fact window up to fact_watermark
├── CompletionValidationResult, if present
└── Task/Attempt limits and counters
```

Rule 不直接读取实时 Business System、原始 Neutral Trace、Hermes 私有对象或当前可变数据库状态。

## 3. Output

每个 Rule 返回一个 `RuleEvaluation`：

```text
pass
finding
indeterminate
not_applicable
rule_error
```

示例：

```json
{
  "rule_evaluation_id": "rule-eval-1",
  "rule_id": "astra.cross_attempt_progress",
  "rule_version": "1",
  "status": "finding",
  "findings": [
    {
      "finding_id": "finding-1",
      "finding_type": "cross_attempt_no_progress",
      "severity": "medium",
      "message": "Two attempts produced no new task-level progress.",
      "fact_refs": ["fact-1", "fact-9"],
      "record_refs": ["attempt-1", "attempt-2"],
      "requirement_refs": ["complaint_ticket_created"]
    }
  ]
}
```

Finding 不包含 `recommended_action`。动作只能由 Task Policy 决定。

## 4. Purity and registration

Rule 必须：

- 确定性、无副作用、可重放；
- 不修改 Task、Attempt、Execution、Receipt、Interaction 或 Fact；
- 不触发 Tool、Interaction、reconciliation 或新的 Attempt；
- 不依赖 Hermes 固定错误字符串；
- 使用稳定 `rule_id` 和显式 `rule_version`；
- 相同 DecisionContext 和 Snapshot 产生相同输出。

第一版使用静态 Python registry 或固定配置映射，不建设规则 DSL、动态策略语言或远程插件平台。新增 Rule 不应要求修改中央 Detector switch。

## 5. Rule semantics

### Cross-attempt no progress

Task-level progress 只包括：

- 新增 satisfied Requirement；
- 新增有效 Receipt 或 confirmed ExternalOperation；
- 权威业务状态发生与 Contract 相关的变化；
- 必要 Interaction 被解决；
- 新增可验证结果或解除 reconciliation。

Tool 调用次数、Provider 切换、模型输出变长或重试次数不算 Task progress。

### Duplicate side effect

相同 idempotency key 返回同一个 Receipt/ExternalOperation 不算重复。Rule 必须依据规范化 effect identity、业务对象和 confirmed operation 判断，不按 Tool 调用次数判断。

### Late state-affecting record

Task 终态后出现新的 confirmed ExternalOperation、Receipt、Interaction resolution 或影响 Completion 的业务状态时产生 Finding。普通日志、usage 和 Trace 不属于 state-affecting record。

### Recovery divergence

新 Attempt 或重启恢复后，Task/Attempt snapshot、Receipt、ExternalOperation 与最新业务 Observation 无法形成一致解释时产生 Finding；证据不足时返回 `indeterminate`。

### Receipt/business-state mismatch

仅检测通用治理矛盾，例如 Receipt 声明操作成功但 authoritative observation 长期确认对象不存在。Complaint 字段是否满足具体 Requirement 由相应 Evaluator 判断，不在 Rule 中重复实现。

## 6. Explicit non-overlap

Completion Validator 直接负责 submitted result 是否满足 Completion Contract、`unknown` 和 `evaluator_error`。Task Rule 不再生成语义重复的 `completion_claim_conflict` 或 `false_completion` Finding。

`no pending blocking Interaction`、`no unresolved reconciliation`、版本匹配和 required ExternalOperation confirmed 属于 Runtime completion invariants，不需要包装成 Finding。

## 7. Contract tests

- 未知 Hermes source code 不改变基于权威记录的 Rule 结果；
- 新 Rule 注册不修改中央 Rule runner；
- 相同 Snapshot 重放输出相同；
- 缺少业务证据返回 `indeterminate`；
- Rule 无写接口和 Tool 接口；
- Completion unsatisfied 不被 Rule 重复计算；
- idempotent replay 不被误判为 duplicate side effect。
