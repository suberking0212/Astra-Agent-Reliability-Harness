# Phase 3 Task Policy Contract

> 状态：`FROZEN / 2026-07-20`  
> 旧 `Recovery Engine` 和 `RecoveryDirective` 设计被本契约取代。Task Policy 只决定 Task 生命周期动作，不决定 Hermes 的 Tool、参数、调用顺序或业务计划。

## 1. Input

```text
TaskPolicyContext
├── DecisionContext
├── authoritative Task/Attempt snapshot
├── EvidenceSnapshot
├── CompletionValidationResult
├── RuleEvaluations / Findings
├── pending Interactions and reconciliation state
└── Task/Attempt limits and counters
```

Policy 不直接读取实时业务系统、原始 Neutral Trace、Hermes 私有对象或固定 Provider error string。

## 2. Allowed actions

第一版动作集：

```text
complete
continue_with_feedback
start_new_attempt
request_input
request_approval
reconcile
fail
escalate
```

- `complete`：请求 Runtime 通过 completion gate 完成 Task；
- `continue_with_feedback`：同一 Attempt 创建新 Execution，沿用 Hermes Session；
- `start_new_attempt`：原子结束旧 Attempt 并创建新 Attempt，默认使用新 Hermes Session；
- `request_input`：请求 Runtime 创建 user-input Interaction；
- `request_approval`：请求 Runtime 创建 approval Interaction；
- `reconcile`：请求只读 Evidence Collection/对账，不执行补偿副作用；
- `fail`：Task 和当前 Attempt 进入失败；
- `escalate`：Task failed，记录 `escalation_required=true`。

已经存在 pending Interaction 时，Runtime 根据权威 Interaction 状态进入等待，不需要 Policy 再输出 `wait_for_existing_interaction`。

## 3. PolicyDecision

```json
{
  "decision_id": "decision-1",
  "decision_key": "sha256:...",
  "policy_id": "astra.default_task_policy",
  "policy_version": "1",
  "task_id": "task-1",
  "attempt_id": "attempt-1",
  "expected_task_version": 12,
  "expected_attempt_version": 5,
  "action": "continue_with_feedback",
  "reason_code": "completion_requirements_unsatisfied",
  "finding_refs": [],
  "evaluation_refs": ["evaluation-1"],
  "fact_refs": ["fact-1"],
  "feedback": {
    "facts": ["No confirmed complaint ticket exists for the submitted identifier."],
    "unmet_requirements": ["complaint_ticket_created"],
    "active_constraints": ["Do not create more than one complaint ticket for this task."],
    "suggestions": ["Re-evaluate the task using the supplied authoritative evidence."]
  }
}
```

PolicyDecision 是不可变建议，不直接写 Task。Runtime 通过 CAS 和唯一 decision key 应用；重复求值必须复用已有 Decision。

## 4. Feedback boundary

`continue_with_feedback` 只能传递：

- 权威事实摘要和证据引用；
- 未满足 Requirement；
- 已由 Constraint Boundary 强制执行的 active constraints；
- 非命令式 suggestions。

Feedback Schema 必须禁止：

```text
next_tool
tool_arguments
ordered_steps
workflow
business_plan
call X then Y
```

事实或约束可以引用 Tool 名称，但不能指定正常业务执行序列。Hermes 独立解释反馈并决定是否以及如何重新规划。

## 5. Bounds

Policy 必须遵守：

```text
max_attempts
task_deadline
max_executions_per_attempt
attempt_deadline
max_feedback_cycles
max_reconcile_cycles
```

超过上限后只能 `fail` 或 `escalate`，不能继续创建 Execution、Attempt 或 reconciliation。

## 6. Interaction responsibility

Policy 可以请求 Interaction，但不能直接创建。`request_input` / `request_approval` 必须包含结构化 `interaction_spec`：

```text
kind
reason_code
required_information, for user input
approval_requirement_ref, for approval
effect_identity, for approval
effect_request_hash, for approval
permission_scope, for approval
evidence_refs
active_constraints
```

`request_approval` 必须引用已经由 Effect Normalizer 固定的 exact effect request。Policy 不得用 Tool 名称或自然语言 `approval_subject` 扩大授权范围，也不签发 Approval/Credential。Approval binding 和消费语义由 `APPROVAL_BINDING.md` 定义。

Runtime 在应用 Decision 的同一事务创建 Interaction、修改 Task/Attempt 状态、写 Fact/Outbox。Rule、Evaluator 和 Aggregator 无权创建 Interaction。

## 7. Normal decision mapping

```text
completion satisfied + Runtime invariants satisfied
→ complete

completion unsatisfied + correctable + feedback budget remains
→ continue_with_feedback

current Attempt cannot continue + Task retry budget remains
→ start_new_attempt

evidence insufficient or external operation indeterminate
→ reconcile

required user fact missing
→ request_input

required governance approval missing
→ request_approval

non-retryable or budget exhausted
→ fail | escalate
```

## 8. Contract tests

- Policy 不依赖 Hermes error string；
- Feedback 拒绝 Tool plan 字段；
- PolicyDecision 不直接改变状态；
- 相同 DecisionContext 产生相同 decision key；
- stale expected version 被 Runtime 拒绝；
- 同一 Decision 不能创建两个 Attempt 或 Interaction；
- reconcile 不获得业务写接口；
- complete 必须通过 Runtime completion gate。
- request_approval 缺少 exact effect identity/request hash 时被拒绝；
- Approval stale 不要求新增 Policy action。
