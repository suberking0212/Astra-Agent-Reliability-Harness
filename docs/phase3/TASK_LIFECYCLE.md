# Phase 3 Task Lifecycle Contract

> **Documentation Governance**
> - **Role:** Normative Task/Attempt/Execution/Interaction domain lifecycle specification.
> - **Authority:** A3 — Runtime Design / Domain Lifecycle.
> - **Topic:** DESIGN.GOVERNANCE.LIFECYCLE
> - **Scope:** Stable identities, states, transitions, interaction ownership, completion, and escalation boundaries.
> - **Not Responsible For:** Durable queue mechanics, checkpoint/recovery implementation, Workflow design, or Python stack restoration.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.HERMES_ASTRA_BOUNDARY
> - **Status:** FROZEN

> 状态：`FROZEN / 2026-07-20`  
> 本文只定义 Task、Attempt、Execution 和 Interaction 的生命周期契约，不实现调度器、Workflow Engine 或 Python 调用栈恢复。

## 1. Stable identity model

```text
Task
└── Attempt 1
    ├── Execution 1
    ├── Interaction wait/resume
    └── Execution 2
└── Attempt 2
    └── Execution 3
```

- **Task**：由 Task Contract 和 Completion Contract 定义的持久化业务目标；
- **Attempt**：Astra 授权的一次有界任务尝试，可包含多个 Execution；
- **Execution**：一次 Hermes Executor Adapter 调用，即一次 `run_conversation()` 执行；
- **Hermes turn**：Hermes 内部概念，不进入 Astra 公共领域契约。

`continue_with_feedback` 和 Interaction 恢复在同一 Attempt 中创建新 Execution。`start_new_attempt` 必须创建新的 Attempt；Provider retry、Tool retry 和参数修复不创建 Astra Attempt。

## 2. Task state machine

第一版 Task 状态：

```text
pending
running
waiting_input
waiting_approval
reconciling
succeeded
failed
cancelled
```

终态为 `succeeded`、`failed`、`cancelled`。终态后禁止普通状态转换；迟到的状态影响记录只能触发审计或 reconciliation。

主要合法转换：

```text
pending → running
running → waiting_input | waiting_approval | reconciling
running → succeeded | failed | cancelled
waiting_input → running | cancelled
waiting_approval → running | failed | cancelled
reconciling → running | succeeded | failed | cancelled
```

Task row 必须包含单调递增 `version`。所有 PolicyDecision 应用必须通过 expected version CAS。

## 3. Attempt state machine

```text
active
waiting
reconciling
completed
failed
exhausted
superseded
cancelled
```

- `completed`：Task 在该 Attempt 内成功；
- `failed`：Attempt 因不可恢复原因失败；
- `exhausted`：Attempt 达到 execution、feedback、reconcile 或 deadline 上限；
- `superseded`：Policy 主动结束旧 Attempt 并创建新 Attempt；
- `cancelled`：Task 取消导致 Attempt 终止。

`start_new_attempt` 必须在一个本地事务内完成旧 Attempt 终结、新 Attempt 创建、PolicyDecision applied 标记、Fact 和可选 Outbox 写入。新 Attempt 应使用新的 Hermes Session；只继承 Task Contract、权威记录引用、固定 Evidence、Receipt、Interaction resolution 和结构化反馈。

Attempt row 必须包含 `version` 以及：

```text
max_executions_per_attempt
attempt_deadline
max_feedback_cycles
max_reconcile_cycles
```

Task 级必须包含 `max_attempts` 和 `task_deadline`。这些上限防止 `continue_with_feedback → new execution` 形成 Task 级无限循环。

## 4. Execution state machine

Phase 3 设计语义使用：

```text
running
completed
failed
interrupted
waiting_input
waiting_approval
limit_exceeded
cancelled
```

除 `running` 外均为终结状态并必须有 `ended_at`。`waiting_input` / `waiting_approval` 表示当前 Execution 已经结束，Task/Attempt 进入等待状态；它不表示 Python 调用栈仍被暂停。

Execution 的 `completed` 只表示 Adapter/Hermes invocation 正常结束，不表示 Task 已完成。Phase 2 当前 `succeeded` 字段保持 baseline 不变，Phase 3 实现前另行设计迁移。

## 5. Interaction ownership

Interaction 可以由以下来源请求：

- Hermes Adapter：Hermes 主动请求用户输入或审批；
- Tool Gateway：确定性审批约束要求；
- Task Policy：任务级 `request_input` 或 `request_approval` 决策。

无论请求来源是谁，只有 Task Runtime 可以持久化 Interaction，并在同一事务修改 Task/Attempt 状态、记录 Fact 和可选 Outbox。Rule、Evaluator 和 Aggregator 无权创建 Interaction。

Approval Interaction 必须遵守 `APPROVAL_BINDING.md`：正式 ApprovalRequest 绑定 Task Contract 中的 approval requirement、CanonicalEffectRequest、`effect_identity` 和 `effect_request_hash`。只包含 Tool 名称或宽泛自然语言、尚未形成精确 effect request 的请求不得产生可执行 ApprovalCredential；信息不足时先使用 user-input Interaction。

Approval resolution 后仍通过新的 Execution 恢复，不恢复 Python 调用栈。Contract 或 effect identity 变化会使旧 Approval stale，但仅 Execution/Attempt retry 不改变同一 effect 的批准身份。

## 6. Completion and escalation

Task 进入 `succeeded` 必须满足 Completion Contract、Runtime completion invariants 和版本 CAS。Hermes `completed` 或单个 valid ResultReceipt 不能独立推进 Task 成功。

第一版 `escalate` 语义固定为：

```text
Task → failed
Attempt → failed
TaskResult.escalation_required = true
```

若需要等待人工决定，应使用 `request_approval` 并进入 `waiting_approval`。第一版不引入含义不清的 `waiting_human`。

## 7. Non-goals

- 不恢复当前 Python 调用栈；
- 不把 Task 转换为 Workflow/DAG；
- 不把 Hermes retry 映射为 TaskAttempt；
- 不让 Policy 或 Rule 直接执行 Tool；
- 不在本阶段实现完整调度、队列或前端状态管理。
