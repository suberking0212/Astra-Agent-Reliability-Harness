# Phase 4 Acceptance Matrix

> **Documentation Governance**
> - **Role:** Frozen Phase 4 P4.0 acceptance specification.
> - **Authority:** A5a — Engineering Acceptance / Phase 4 P4.0.
> - **Topic:** ACCEPTANCE.ENGINEERING.P4
> - **Scope:** P4-A01–P4-A13 acceptance IDs, semantic assertions, crash/concurrency matrix, milestones, and required freeze artifacts.
> - **Not Responsible For:** Creating product or runtime requirements, reporting actual pass/fail status, defining repository-wide release readiness, or remediating defects.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.PERSISTENT_TASK_RUNTIME, DESIGN.RUNTIME.DURABLE_TASK
> - **Status:** FROZEN

> 状态：`FROZEN / P4.0 / 2026-07-20`  
> 范围：Phase 4 首版顶层验收、测试矩阵与冻结产物  
> 运行时架构：`docs/adr/ADR-004-persistent-task-runtime.md`

## 1. 使用规则

- 顶层验收编号固定为 `P4-A01` 至 `P4-A13`；
- 每个 Phase 4 实现提交必须引用至少一个编号；
- 每项验收必须由自动化测试证明，不以手工演示代替；
- crash window 和并发竞态作为测试矩阵展开，不新增顶层产品能力；

推荐提交说明格式：

```text
feat(runtime): persist task submission and initial attempt

Implements: P4-A01, P4-A02
```

## 2. 顶层验收

| ID | 验收能力 | 必须证明的结果 | 主要测试类型 | 计划里程碑 |
|---|---|---|---|---|
| P4-A01 | 生命周期持久化 | Task、Attempt、Execution、Interaction 在数据库关闭和新进程重开后 identity、version、state 与 Contract 引用完整保留；旧数据库可原地迁移。 | migration、store integration、subprocess restart | M1 |
| P4-A02 | 命令幂等 | 相同 `command_id` 与相同 payload 返回首次语义结果，不创建重复生命周期记录或 Run Request；相同 ID、不同 payload 返回 identity conflict。 | unit、transaction、restart replay | M1 |
| P4-A03 | `waiting_input` 恢复 | 重启后可解决 pending input Interaction；resolution、Task/Attempt 推进和 Run Request 创建在同一事务；同一 Attempt 创建新 Execution。 | store integration、subprocess restart、race | M3 |
| P4-A04 | `waiting_approval` 恢复 | 重启后可保存精确 ApprovalResolution 并继续；执行 ExternalOperation 的 `effect_identity` 与批准值完全一致，stale/mismatch approval 被拒绝。 | approval contract、gateway integration、subprocess restart | M3 |
| P4-A05 | cancel 持久化与幂等 | cancel intent 在重启后仍有效；重复 cancel 不产生重复记录；cancel/complete 由首先成功的 Task version CAS 决定。 | transaction、restart、CAS race | M3 |
| P4-A06 | cancel 后副作用保护 | cancelled Task 的 Gateway 新副作用请求被拒绝；迟到结果不覆盖终态；可能已 dispatch 的未知操作进入 reconciliation。 | gateway integration、late-result、reconciliation | M3-M4 |
| P4-A07 | orphaned Execution 终结 | 失去有效执行者的 running Execution 被确定性识别并终结为 `interrupted / process_lost`。 | subprocess kill、startup recovery | M4 |
| P4-A08 | 新 Execution 恢复 | restart recovery 创建新 Run Request 和新 Execution，不继续原 Execution，不恢复 Python 调用栈；session handle 仅作为持久化上下文引用使用。 | subprocess kill、identity assertion、E2E | M4 |
| P4-A09 | 未知外部操作先对账 | `prepared + dispatch_status_unknown`、`in_flight`、`acknowledged`、`indeterminate` 先 reconciliation；只有 `prepared + confirmed_not_dispatched` 可使用同一 effect identity 安全继续。 | operation-state table、crash recovery、reconciliation | M4 |
| P4-A10 | 单一 claim | 两个独立进程同时竞争同一 Run Request 时，只有一个获得有效 claim/lease token。 | multiprocessing race、SQLite integration | M5 |
| P4-A11 | fencing | lease takeover 后，旧 Worker 的 heartbeat、result 和 finalize 写入因 stale token 被拒绝；旧所有权不能覆盖新 Worker 状态。 | multiprocessing takeover、stale writer | M5 |
| P4-A12 | ExternalOperation 唯一性 | 相同 `authority_domain + effect_identity` 在 Execution、Attempt、Worker 或进程变化后仍只对应一个 ExternalOperation。 | concurrent prepare、restart replay、unique constraint | M4-M5 |
| P4-A13 | budget 与 deadline | Task/Attempt budget 和 deadline 阻止无限 Execution/Attempt；deadline 固定为失败，使用稳定 termination reason。 | boundary、restart、policy integration | M2-M4 |

## 3. 验收语义断言

### P4-A05 cancel/complete 竞争

```text
cancel CAS 先提交
→ Task cancelled
→ late completion audit-only

completion CAS 先提交
→ Task succeeded 或 failed
→ cancel 返回 terminal_conflict
```

不得使用时间戳决定胜者。

### P4-A03 resolve/cancel 竞争

```text
cancel 事务先提交
→ Interaction cancelled
→ resolve 不创建 Run Request

resolve 事务先提交
→ resolution + Task/Attempt 推进 + Run Request 同事务提交
→ 后续 cancel 可取消 Task 和尚未开始的 Run Request
```

### P4-A09 prepared operation 分类

```text
prepared + confirmed_not_dispatched
→ safe continue with same effect_identity

prepared + dispatch_status_unknown
→ reconciliation

in_flight / acknowledged / indeterminate
→ reconciliation
```

无法证明未 dispatch 时必须保守对账。

### P4-A13 deadline 与预算原因

```text
task_deadline_exceeded
attempt_deadline_exceeded
execution_budget_exhausted
attempt_budget_exhausted
```

Task deadline 到期必须进入 `failed`，不得映射为 `cancelled`。

## 4. Crash-point matrix

Crash 测试必须使用真实 SQLite 文件、独立子进程和强制进程退出。不得仅以抛出
异常或 mock rollback 代替进程崩溃。

| Crash ID | 强杀位置 | 恢复后必须满足 | 覆盖验收 |
|---|---|---|---|
| P4-C01 | schema migration 完成前/后 | 失败迁移阻止 Worker；成功迁移可重复执行且数据保留。 | A01 |
| P4-C02 | Task/Attempt/Run Request 原子提交前/后 | 不出现半个初始生命周期；重放 `submit_task` 返回同一结果。 | A01、A02 |
| P4-C03 | Run Request claim 后、Execution 创建前 | 过期执行权可恢复；不产生两个有效 claim。 | A07、A08、A10 |
| P4-C04 | Execution 创建后、Hermes 调用前 | 原 Execution 终结为 interrupted；恢复使用新 Execution。 | A07、A08 |
| P4-C05 | 外部调用边界前后 | 已证明未 dispatch 可继续；dispatch 未知或可能已发生则先 reconciliation。 | A06、A09、A12 |
| P4-C06 | ExecutionResult 保存后、Governance 应用前 | 结果不丢失；治理可幂等重放；Task 不被 Hermes 输出直接完成。 | A02、A08 |
| P4-C07 | PolicyDecision 保存后、应用前 | 重启后复用同一 decision/context；CAS stale 时不得强行应用。 | A02、A13 |
| P4-C08 | Interaction resolution 事务前/后 | 不出现 resolved Interaction 缺少 Run Request 的已提交中间态；重放幂等。 | A02、A03、A04 |
| P4-C09 | cancel 事务前/后 | 已提交 cancel 跨重启生效；未提交 cancel 不伪造取消；重放幂等。 | A05、A06 |
| P4-C10 | 权威状态提交后、Outbox 投递前 | 状态保持；Outbox 可幂等恢复投递。 | A01、A08 |

## 5. Multi-process matrix

多进程测试使用同一主机上的两个或更多独立进程，共享一个本地 SQLite 文件。

| Race ID | 并发场景 | 必须满足 | 覆盖验收 |
|---|---|---|---|
| P4-R01 | 两进程同时 claim 同一 Run Request | 只有一个有效 token，另一个不执行。 | A10 |
| P4-R02 | active lease 期间第二进程 claim | 第二进程被拒绝，不发生提前 takeover。 | A10 |
| P4-R03 | lease 到期后 takeover | 新 Worker 获得新 token，旧 Worker 失去写入权。 | A07、A08、A11 |
| P4-R04 | 旧 Worker 与新 Worker 同时 finalize | 只有当前 token 的写入可提交。 | A11 |
| P4-R05 | cancel 与 completion 并发 | 只有第一个 Task version CAS 成功，终态不被覆盖。 | A05 |
| P4-R06 | resolve 与 cancel 并发 | 遵守事务胜者规则，不产生 terminal Task 的可执行 Run Request。 | A03、A05 |
| P4-R07 | 两进程重复执行同一 command_id | 返回同一命令结果，不产生重复对象。 | A02 |
| P4-R08 | 两进程准备同一 effect identity | 只存在一个 ExternalOperation。 | A12 |
| P4-R09 | 两进程应用同一 PolicyDecision | decision 只应用一次，派生记录不重复。 | A02 |

## 6. Milestones

| Milestone | 阻塞验收 |
|---|---|
| P4.0 | Phase 4 三份规范文档职责一致并服从 `docs/AUTHORITY.md`；A01-A13 编号和四处唯一语义冻结。 |
| M1 Persistent Runtime Core | A01、A02。 |
| M2 Single-worker Execution | A01、A02、A13，并形成 submit→execute→governance 闭环。 |
| M3 Interaction and Cancellation | A03、A04、A05、A06。 |
| M4 Checkpoint and Recovery | A07、A08、A09、A12、A13，完成 crash matrix。 |
| M5 Multi-process Coordination | A10、A11、A12，完成 race matrix。 |
| M6 Freeze | A01-A13 全部通过，文档和 artifacts 完整。 |

## 7. Freeze artifacts

Phase 4 正式冻结输出保存到：

```text
artifacts/phase4-task-runtime-complete/
```

至少包含：

```text
README.md
acceptance-manifest.json
phase4-acceptance.txt
crash-recovery.txt
multi-process-race.txt
ruff.txt
```

`acceptance-manifest.json` 至少记录：

- Git commit 和 Phase 4 tag；
- schema version；
- A01-A13 每项状态及对应测试；
- crash/multi-process suite 结果；
- SQLite 同主机多进程适用边界；
- 已知非阻塞环境依赖。

只有 A01-A13 全部通过、验收输出归档完成后，Phase 4
才能标记为 `COMPLETE AND FROZEN`。
