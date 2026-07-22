# Phase 4 Durable Astra Task Runtime

> 状态：`FROZEN / P4.0 / 2026-07-20`  
> 适用范围：Phase 4 首版实现与验收  
> 权威决策：`docs/adr/ADR-004-persistent-task-runtime.md`  
> 验收矩阵：`docs/phase4/ACCEPTANCE_MATRIX.md`

## 1. 阶段定位

Phase 4 在 Phase 3 冻结基线上实现一套可持久化、可调度、可等待、可取消、
可跨进程恢复，并支持同一主机多个独立进程安全竞争的 Astra Task Runtime。

正式范围只包含仓库已声明的能力：

```text
Task persistence
TaskAttempt
waiting_input
waiting_approval
cancel
checkpoint
restart recovery
queue / scheduling
multi-instance coordination
```

首版纵向主线固定为：

```text
持久化 Task / Attempt / Execution / Interaction
→ 持久化 Run Request
→ 单 Worker 执行
→ waiting_input / waiting_approval
→ cancel
→ checkpoint
→ restart recovery
→ lease、heartbeat 与多进程 fencing
```

Phase 4 的目标是一个可以在本阶段完成、验证并冻结的 Durable Task Runtime，
不是跨主机分布式任务平台。

## 2. 架构边界

Phase 4 必须保持以下职责划分：

```text
Hermes
= Agent Execution Logic

Astra Task Runtime
= Task Execution Lifecycle

RuntimeGovernanceCore
= Governance Authority

AstraStore
= 唯一权威持久化与本地事务边界
```

- Hermes 负责模型推理、Tool 选择、参数生成、局部 retry、动态 replanning 和
  单次 Agent Loop；
- Astra Task Runtime 负责 Task、Attempt、Execution、Interaction、Run Request、
  cancel、checkpoint、recovery 和 Worker ownership；
- RuntimeGovernanceCore 继续负责 Task Policy、Completion Gate、治理决策及其
  CAS 应用；
- AstraStore 继续承载权威记录、幂等约束和原子事务；
- Tool Gateway 继续负责权限、审批、幂等、副作用身份、ExternalOperation 和
  Task cancel 检查。

核心边界继续采用：

> Hermes 决定如何执行；Astra 决定是否允许、是否满足 Task Contract，以及
> 哪些事实可以被正式承认。

Phase 4 不修改 Hermes Agent Loop，不复制 RuntimeGovernanceCore，不创建第二套
Runtime authority，也不把 Task 生命周期实现为 Workflow、DAG 或固定业务步骤。

现有 `Phase2Runtime` 可以作为兼容入口或薄适配层调用新增能力；删除、改名或
搬迁它不是 Phase 4 验收目标。

## 3. 首版部署与并发边界

Phase 4 第一版只承诺：

```text
同一主机
多个独立 Runtime Worker 进程
共享一个本地 SQLite 数据库
使用数据库事务、CAS、lease 和 fencing 协调
```

首版不承诺跨主机协调，不支持把 SQLite 放在无法保证其锁和一致性语义的网络
文件系统上，也不引入 Kafka、Redis、Celery 或其他分布式协调设施。

## 4. 稳定身份与生命周期

```text
Task
└── Attempt 1
    ├── Execution 1
    ├── Interaction wait/resume
    └── Execution 2
└── Attempt 2
    └── Execution 3
```

### 4.1 Task

Task 是由 Task Contract 和 Completion Contract 定义的持久化业务目标。首版
必须持久化稳定 identity、精确 Contract 引用、状态、单调递增 version、当前
Attempt、Task budget/deadline 和取消/终结事实。

Task 状态沿用 Phase 3 冻结定义：

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

`succeeded`、`failed` 和 `cancelled` 是终态。普通命令不得重新打开终态 Task；
迟到结果只能形成审计记录，或者在存在外部状态不确定性时触发 reconciliation。

### 4.2 TaskAttempt

Attempt 是 Astra 授权的一次有界任务尝试。它必须具有稳定 identity、Task 内
ordinal、状态、单调递增 version、Execution 数量限制、Attempt deadline 和
终结原因。

Attempt 状态沿用：

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

`continue_with_feedback` 和 Interaction 恢复在同一 Attempt 中创建新 Execution。
只有明确的 `start_new_attempt` 治理决定或冻结的 attempt-exhaustion 规则才创建
新 Attempt。

以下行为不创建 Astra Attempt：

- Provider retry；
- Tool retry；
- 参数修正；
- Hermes 单轮内部 replanning；
- Interaction 解决后的继续执行。

### 4.3 Execution

Execution 表示一次独立的 Hermes Executor Adapter 调用。它必须持久化稳定
identity、所属 Task/Attempt、运行状态、当前 Worker ownership、开始/结束时间、
termination reason、Hermes session handle、Checkpoint 引用和迟到结果判断所需
的最小信息。

Phase 4 不主动迁移现有 Execution `succeeded` 命名。状态名称只有在新的验收项
证明存在实际语义冲突时才允许迁移；术语整洁本身不是迁移理由。

### 4.4 Interaction

Interaction 是持久化的输入或审批等待，首版支持：

```text
kind: user_input | approval
status: pending | resolved | cancelled
```

Interaction resolution 必须幂等、受 CAS 保护、可以跨重启，并在同一 Attempt
中创建新的 Execution。它永远不恢复旧 Python 调用栈。

## 5. Schema version 与迁移

Phase 4 必须为 SQLite schema 建立显式版本，并支持现有 Phase 2/3 数据库原地
升级。

迁移规则固定为：

- 原地执行且幂等；
- 不删除或静默重建历史数据；
- 迁移失败时 Worker 不得开始 claim；
- 每次迁移均验证迁移前提与迁移后 schema；
- 保留现有 AstraStore 事务边界；

Phase 4 冻结行为不变量，不提前冻结完整生产字段集合。字段只有在某个验收项
需要、实际参与 Runtime 判断，并能在同一纵向切片内完成实现和测试时才加入。

## 6. 幂等命令与 CAS

所有改变生命周期的外部 Runtime 命令必须携带稳定 `command_id`。首版至少覆盖：

```text
submit_task
resolve_interaction
cancel_task
record_execution_result
recover_execution
```

命令处理顺序固定为：

```text
加载权威状态
→ 验证 command identity
→ 验证 expected version
→ 验证合法状态转换
→ 在同一事务中写入状态与派生记录
→ 保存并返回稳定命令结果
```

相同 `command_id` 和相同 payload 必须返回第一次执行的语义结果，不得创建重复
Task、Attempt、Execution、Interaction 或 Run Request。相同 ID、不同 payload
必须返回 identity conflict。

Task 和 Attempt 的业务状态转换使用 version CAS。Worker lease 只表示执行所有权，
不能替代 Task/Attempt CAS、PolicyDecision 幂等或 Completion Gate。

## 7. Run Request 与首版调度

Run Request 表示一次已经由 Task 生命周期授权、等待 Worker 执行的 Hermes
invocation 请求。它不是 Workflow step，也不规定 Tool、参数或业务执行顺序。

首版只冻结以下行为：

- Run Request 具有稳定 identity 和 Task/Attempt 引用；
- 支持 `pending`、`claimed`、`completed`、`cancelled`；
- 支持 priority、ready time 和确定性创建顺序；
- 同一生命周期触发只产生一个 Run Request；
- terminal/cancelled Task 的 Run Request 不可执行；
- claim、完成和取消均为事务性状态转换。

允许的触发原因包括：

```text
task_submitted
interaction_resolved
continue_with_feedback
new_attempt
restart_recovery
reconciliation_completed
```

首版排序固定为：

```text
priority DESC
ready_at ASC
created_at ASC
```

Task deadline 是执行资格检查，不要求参与首版排序。首版不实现 aging、饥饿避免、
复杂 deadline priority、调度 DSL、多队列路由或能力匹配调度。

## 8. 单 Worker Execution 闭环

多进程协调前必须先完成：

```text
submit Task
→ 同事务创建 Task、初始 Attempt 和 Run Request
→ 单 Worker claim
→ 创建 Execution
→ 构造 RuntimeInvocation
→ HermesExecutor 执行
→ 持久化 ExecutionResult
→ Completion / Governance 求值
→ Task 终态或下一 Run Request
```

Execution Driver 必须在执行前重新检查 Task/Attempt 状态、budget 和 deadline，
加载 Task Contract、session handle 和持久化 feedback，并在执行结束后通过现有
治理链推进 Task，而不是依据 Hermes 的自然语言或 `completed=true` 直接完成 Task。

## 9. waiting_input 与 waiting_approval

### 9.1 waiting_input

```text
Hermes 或 Policy 请求输入
→ Runtime 持久化 Interaction
→ 当前 Execution 终结
→ Task/Attempt 进入等待
→ 进程可安全退出
→ resolve_interaction
→ 同事务解决 Interaction、推进 Task/Attempt、创建 Run Request
→ 同一 Attempt 创建新 Execution
→ 注入 InteractionResolution feedback
```

### 9.2 waiting_approval

```text
CanonicalEffectRequest
→ 精确匹配 ApprovalRequirement
→ ApprovalRequest + Interaction
→ Task waiting_approval
→ ApprovalResolution
→ resolve_interaction
→ 新 Run Request / 新 Execution
→ Gateway 验证 exact approval binding
```

批准的 `effect_identity` 必须等于执行的 ExternalOperation `effect_identity`。
宽泛自然语言审批不能产生可执行授权；Contract、effect intent、subject、规范化
参数、authority domain、permission scope 或 Approval Requirement 变化会使旧批准
stale。

### 9.3 resolve 与 cancel 竞争

该竞争只由数据库事务和 CAS 决定，不使用时间戳判断：

- cancel 事务先成功：Interaction 被取消，后续 resolve 不创建 Run Request；
- resolve 事务先成功：Interaction resolution、Task/Attempt 状态推进和新 Run
  Request 必须在同一事务内提交；随后 cancel 仍可取消 Task 和尚未开始的 Run
  Request。

## 10. Cancel

取消首先是持久化权威事实，其顺序固定为：

```text
cancel_task(command_id)
→ Task version CAS
→ 持久化 cancel intent
→ Task cancelled
→ 当前 Attempt cancelled
→ 未开始 Run Request cancelled
→ pending Interaction cancelled
→ best-effort AgentExecutor.cancel()
```

Runtime 必须先提交取消事务，再通知进程内 Executor。重复 cancel 必须幂等。

Task 取消后：

- Gateway 拒绝新的副作用；
- 尚未越过调用边界的本地 prepared 操作不得被发送；
- 已经可能到达外部系统的操作不能假设被撤销；
- 迟到结果只能审计，不能覆盖 `cancelled`；
- 外部状态不确定时进入 reconciliation。

### 10.1 cancel 与 completion 竞争

该竞争以首先成功提交 Task version CAS 的事务为准：

- cancel 先成功：Task 为 `cancelled`，迟到 completion 只记录审计，必要时触发
  reconciliation；
- completion 先成功：Task 为 `succeeded` 或 `failed`，后续 cancel 返回
  `terminal_conflict`，不得覆盖终态。

不使用请求时间、模型返回时间或其他时间戳判断谁先发生。

## 11. Checkpoint

Checkpoint 是安全生命周期边界上的持久化恢复信封，不是 Python 调用栈、Hermes
内部执行快照、token 级快照、Tool 步骤列表或 Workflow 游标。

它只保存恢复下一次 Runtime 决策需要的最小权威引用，例如 Task/Attempt/Execution
identity 和 version、Task Contract reference、Hermes session handle、已解决
Interaction、持久化 feedback、Evidence/Completion、ExternalOperation/
Reconciliation 引用以及内容 hash。

首版安全边界至少包括：

- Task/Attempt/Run Request 已原子创建；
- Execution 即将开始；
- Execution 已终结；
- Task 进入 waiting；
- Interaction 已解决；
- PolicyDecision 已应用；
- reconciliation 已完成；
- Task 进入终态。

恢复时权威顺序固定为：

```text
数据库权威记录 > Checkpoint > Hermes session context
```

Checkpoint 不得覆盖更新版本的权威状态。

## 12. Restart recovery

Runtime 启动时至少扫描：

- 已失去有效执行权的 running Execution；
- 已解决但缺少 Run Request 的 Interaction；
- running Task 缺少可执行工作；
- pending PolicyDecision 和 pending Outbox；
- 未完成 reconciliation；
- 未达到确定终态的 ExternalOperation。

失去执行者的 running Execution 必须终结为：

```text
state = interrupted
termination_reason = process_lost
```

原 Execution 不得继续使用。恢复必须读取持久化事实和 Checkpoint，并创建新的
Run Request 与新的 Execution；不得恢复旧 Python 调用栈。

### 12.1 恢复分类

#### 安全继续

当 Task/Attempt 非终态、未取消、budget/deadline 允许，且不存在可能已发出的
未知副作用时：旧 Execution 终结为 interrupted，在同一 Attempt 创建新 Run
Request 和新 Execution。

#### 先 reconciliation

当外部操作可能已经越过调用边界但结果未确定时：Task/Attempt 进入 reconciling，
查询外部权威状态、固定 EvidenceSnapshot、更新 ExternalOperation，并重新运行
Completion/Policy。不得直接重放副作用。

#### 不允许恢复

Task 已取消、Task/Attempt 已终结、deadline 到期或 budget 耗尽时，不创建普通
恢复 Execution，而是保持或形成相应终态并记录恢复事实。

### 12.2 prepared ExternalOperation

`prepared` 必须结合持久化 dispatch 事实分类：

```text
prepared + confirmed_not_dispatched
→ 可使用同一 effect_identity 和同一幂等语义安全继续

prepared + dispatch_status_unknown
→ reconciliation

in_flight / acknowledged / indeterminate
→ reconciliation
```

如果当前权威数据无法证明操作尚未 dispatch，Runtime 必须保守进入 reconciliation。

## 13. Budget 与 deadline

Phase 4 首版必须强制：

```text
max_attempts
task_deadline
max_executions_per_attempt
attempt_deadline
```

检查至少发生在创建 Attempt、创建 Run Request、claim 后开始 Execution、Interaction
resolution、restart recovery 和 reconciliation 完成时。

deadline 语义固定为失败，不是取消：

```text
Task deadline exceeded
→ Task failed
→ termination_reason = task_deadline_exceeded

Attempt deadline exceeded
→ Attempt exhausted
→ termination_reason = attempt_deadline_exceeded
```

预算耗尽使用稳定原因：

```text
execution_budget_exhausted
attempt_budget_exhausted
```

`cancelled` 只表示明确取消意图。Runtime 不得让 continue/new Execution 或 new
Attempt 形成无界循环。

## 14. Lease、heartbeat 与 fencing

多进程切片中，Worker claim Run Request 时原子写入 owner、不可复用的 lease token
和 expiry。同一时刻只有一个 token 有效。

首版要求在创建 Execution、开始调用、续租、保存 terminal result 和 finalize Run
Request 前验证当前 token：

- 短 Execution 可以依赖初始 lease，并在关键写入前校验；
- 长 Execution 定期续租；
- 不要求首版构建完整 Worker 管理平台。

Lease 到期后其他进程可以 takeover，并获得新 token。旧 Worker 恢复运行后，其
heartbeat、result 和 finalize 写入必须被 fencing 拒绝。接管仍需终结 orphaned
Execution，并根据副作用状态选择安全继续或 reconciliation。

Lease 不能替代 Task/Attempt version CAS、PolicyDecision 幂等、Completion Gate 或
ExternalOperation 的 `authority_domain + effect_identity` 唯一约束。

## 15. 实施里程碑

### P4.0 契约冻结

只维护三份权威文档：本文、`ACCEPTANCE_MATRIX.md` 和 ADR-004。固定最小状态、
13 项验收、SQLite 边界、迁移规则、恢复分类以及本文件中的竞争语义。

### Milestone 1：Persistent Runtime Core

实现 schema migration、Task/Attempt/Execution/Interaction、command identity、CAS
和终态保护。第一条纵向切片固定为：

```text
旧数据库原地迁移
→ submit_task(command_id)
→ 同事务持久化 Task、第一个 Attempt 和 Run Request
→ 关闭数据库
→ 新进程重新打开
→ 验证 identity / version / state
→ 重复 submit 返回原结果
```

该切片完成前不创建空的 scheduler、recovery 或 checkpoint 模块。

### Milestone 2：Single-worker Execution

实现 Run Request claim、Execution Driver、RuntimeInvocation、HermesExecutor、result
submission、Governance 求值以及终态或下一 Run Request。

### Milestone 3：Interaction and Cancellation

实现 waiting_input、waiting_approval、事务性 resolve、同 Attempt 新 Execution、
cancel intent、迟到结果拒绝和 Gateway cancel check。

### Milestone 4：Checkpoint and Restart Recovery

实现 safe-boundary Checkpoint、startup scanner、orphaned Execution、interrupted
termination、新 Execution 恢复、ExternalOperation reconciliation 和 pending
Outbox/Decision 恢复。崩溃验收必须使用真实独立子进程强杀。

### Milestone 5：Multi-process Coordination

实现 lease、expiry、简单 heartbeat、fencing、双进程 claim、takeover 和 stale
finalize rejection。只承诺同一主机共享本地 SQLite。

### Milestone 6：Freeze

完成 Phase 4 当前能力检查、crash-point suite 和 multi-process race suite，保存
本阶段产物并更新状态文档。

## 16. 非目标

- 跨主机分布式协调；
- Kafka、Redis、Celery 或消息总线；
- Workflow Engine、DAG、Workflow Builder；
- 固定 Tool 顺序或业务计划；
- 多 Agent 或 Planner Agent；
- 修改 Hermes Agent Loop；
- 第二套 Governance Core 或权威 Store；
- Python 调用栈恢复或 token 级 Checkpoint；
- 高级调度公平性、aging、复杂 deadline 排序；
- Worker 管理平台；
- Web API、WebUI、Phase 5 Evaluation Oracle 或 Phase 6 Demo；
- 没有 Phase 4 当前需求驱动的改名、搬迁或架构重构。

## 17. 变更纪律

所有 Phase 4 实现提交必须引用 `P4-A01` 至 `P4-A13` 中至少一个验收编号。
Phase 3 实现只能因明确的 Phase 4 功能需要而修改，不得因代码清理、术语偏好
或未来扩展设想进行机会性改动。
