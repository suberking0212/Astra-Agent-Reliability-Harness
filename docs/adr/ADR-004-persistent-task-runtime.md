# ADR-004: Persistent Astra Task Runtime

> 状态：`ACCEPTED / PHASE 4 P4.0 FROZEN`  
> 日期：2026-07-20  
> 适用范围：Phase 4 Durable Task Runtime  
> 运行时契约：`docs/phase4/TASK_RUNTIME.md`  
> 验收矩阵：`docs/phase4/ACCEPTANCE_MATRIX.md`

## Context

Phase 3 已冻结 Task Contract、Completion、Policy、Approval binding、Canonical Effect
Identity、ExternalOperation 和 Runtime CAS 的治理语义，但当前仓库尚未提供完整的
持久化 Task Runtime。Task/Attempt/Execution 的完整生命周期、等待交互、取消、
Checkpoint、进程重启恢复、Run Request 和多进程执行权协调属于 Phase 4。

Phase 4 必须补齐这些运行时能力，同时避免重新实现 Hermes Agent Loop、复制
RuntimeGovernanceCore，或把本阶段扩展为跨主机分布式 Workflow 平台。

## Decision

### 1. AstraStore 是唯一权威存储边界

Phase 4 继续使用现有 AstraStore 及其 SQLite 本地事务作为 Task Runtime 的唯一
权威持久化边界。RuntimeGovernanceCore 继续通过同一事务边界读写治理记录。

不得创建平行的 Phase 4 authority store、第二套 Task 状态机或通过消息投递记录
反推当前 Task 状态。Schema 使用显式版本并原地迁移；迁移失败时 Worker 不得
开始执行。

### 2. Run Request 使用 SQLite 数据库驱动

Phase 4 使用持久化 Run Request 表示一次已由 Task 生命周期授权、等待 Worker
执行的 Hermes invocation。Run Request 不是 Workflow step，不包含固定 Tool 顺序、
业务计划或 DAG 信息。

首版调度只保证：

```text
priority DESC
ready_at ASC
created_at ASC
```

首版不引入外部消息队列或复杂调度算法。

### 3. Restart recovery 创建新 Execution

进程崩溃或重启后，Runtime 不恢复 Python 调用栈，也不继续使用 orphaned
Execution。旧 running Execution 被终结为 `interrupted / process_lost`。

Runtime 根据持久化 Task/Attempt 状态、Interaction resolution、Hermes session
handle、Receipt、ExternalOperation、Evidence、PolicyDecision 和 Checkpoint 创建新的
Run Request 与新的 Execution。可能已 dispatch 但结果未知的副作用必须先
reconciliation，不得直接重发。

### 4. Checkpoint 是持久化恢复信封

Checkpoint 只保存安全生命周期边界上恢复下一次 Runtime 决策所需的最小权威
引用和版本。它不是 Python 栈、Hermes 内部执行快照、token 级快照、Tool 步骤表
或 Workflow 游标。

恢复时权威顺序固定为：

```text
数据库权威记录 > Checkpoint > Hermes session context
```

### 5. 同一主机多进程通过 lease、fencing 和 CAS 协调

Phase 4 第一版只承诺同一主机多个独立进程共享一个本地 SQLite 文件。

- lease 表示 Worker 对 Run Request 的临时执行权；
- heartbeat 为长 Execution 续租；
- 每次 claim/takeover 生成新的 fencing token；
- 旧 token 不能 heartbeat、提交结果或 finalize；
- Task/Attempt version CAS 决定业务状态转换；
- `authority_domain + effect_identity` 唯一约束决定 ExternalOperation 身份。

Lease 不替代 CAS、Completion Gate 或副作用身份约束。本阶段不承诺跨主机协调。

### 6. 不引入 Workflow Engine 或第二套 Governance Core

Hermes 继续拥有 Agent Execution Logic；Astra Task Runtime 只拥有 Task Execution
Lifecycle；RuntimeGovernanceCore 继续是治理权威。

Phase 4 不实现 Workflow Engine、DAG、固定业务步骤、Planner Agent、第二套 Agent
Runtime、Hermes 内部 retry/replanning、第二套 Governance Core，也不把替换或
重命名 Phase2Runtime 作为目标。

## Consequences

- Task 生命周期可以跨进程重启而不依赖 Python 内存；
- waiting、cancel、completion 和 recovery 继续使用同一权威事务与 CAS；
- Run Request 可以先形成单 Worker 闭环，再加入同主机多进程协调；
- SQLite 限定了首版部署范围和并发规模；
- 无法证明外部操作未 dispatch 时必须保守 reconciliation；
- 所有实现必须由 `P4-A01` 至 `P4-A13` 验收编号驱动。

P4.0 后第一条实现切片固定为：原地迁移、幂等 `submit_task(command_id)`、同事务
创建 Task/初始 Attempt/Run Request、跨进程重开验证和重复 submit 验证。在该
切片完成前，不创建空的 scheduler、recovery 或 checkpoint 模块。
