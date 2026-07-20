# Phase 2 Core Baseline

> 日期：2026-07-19  
> 状态：`CORE CLOSED AND BASELINED` / `LIVE PROVIDER PENDING: MISSING CREDENTIALS`

## 1. Baseline 结论

Astra × Hermes 第一条可运行、可验证、可持久化终结的投诉处理纵向闭环已经通过真实 Hermes Plugin discovery、Tool Registry、`AIAgent` Loop、Tool execution、Observer Hooks 和 SessionDB 验证。Hermes Agent Loop 未修改；Astra 只通过 Executor Adapter、固定 Bridge Plugin、Tool Gateway 和持久化端口介入。

## 2. Live Provider smoke test

测试文件：`tests/test_phase2_live_provider.py`。

测试保持默认 skip，不阻塞普通 CI。运行条件：

```bash
export ASTRA_RUN_LIVE_PROVIDER=1
export ASTRA_LIVE_BASE_URL="https://provider.example/v1"
export ASTRA_LIVE_API_KEY="..."
export ASTRA_LIVE_MODEL="model-name"

# 可选
export ASTRA_LIVE_PROVIDER="custom"
export ASTRA_LIVE_API_MODE="chat_completions"

pytest -q tests/test_phase2_live_provider.py -s
```

测试方法：

1. 创建隔离的 `HERMES_HOME`，只启用固定 `astra_bridge` Plugin；
2. 使用真实 Provider 配置构造真实 Hermes `AIAgent`；
3. 注入投诉 Task Contract，只暴露允许的业务 Toolset；
4. 要求真实模型自主查询订单、搜索政策、创建投诉工单并调用 `submit_task_result`；
5. 验证 Mock Business Service 中只产生一个正确工单；
6. 验证 `ResultReceipt.valid=true`、`task_outcome_validated=true`；
7. 验证 `ExecutionResult`、Provider usage 和 Neutral Trace 完整性。

当前结果：未运行。本机未发现任何显式 Provider 凭证或 `~/.hermes/config.yaml`，因此不能安全发起真实网络模型调用。禁止使用隐式、未知来源或无法审计的凭证替代。

## 3. Trace、Receipt 和持久化审计

复验命令：

```bash
python3 scripts/run_phase2_core_audit.py
```

生成目录：`artifacts/phase2-core-runtime-audit/`。

审计结果：

| 项目 | 结果 |
| --- | --- |
| Astra SQLite `integrity_check` | `ok` |
| Astra SQLite foreign keys | 0 violations |
| Execution | 1，状态 `succeeded` |
| `AstraExecutionEnded` | 1 |
| Neutral Trace | 25 spans |
| ExecutionReceipt | 3 |
| ResultReceipt | 1，`valid=true` |
| ComplaintTicket | 1 |
| Provider calls | 5 |
| Observed tokens | 200 |
| Hermes Session | 1 |
| Hermes persisted messages | 10 |
| Hermes SessionDB `integrity_check` | `ok` |

Receipt 顺序：

```text
get_order
→ search_policy
→ create_complaint_ticket
→ submit_task_result references all three receipts
→ ResultReceipt validates actual ticket state
```

Hermes Session 持久化角色序列：

```text
user
assistant → tool
assistant → tool
assistant → tool
assistant → tool
assistant
```

Trace 从 `ExecutionStarted` 开始，以 `ExecutionEnded` 结束；`ResultSubmission` 严格早于 `ResultValidation`。模型只看到 Task Contract 允许的三个业务工具和三个 Runtime primitive，未看到 `get_customer` 或 `get_complaint_ticket`。

## 4. 压力验证

复验命令：

```bash
pytest -q tests/test_phase2_stress.py -s
```

### 4.1 并发终结

512 个竞争者使用独立 SQLite connection 同时终结同一 Execution：

```text
winner = 1
loser = 511
AstraExecutionEnded = 1
integrity_check = ok
```

### 4.2 暂停后副作用

持久化 `InteractionRequest` 并设置 `suspension_requested=true` 后，并发发起 512 次创建投诉工单：

```text
execution_suspended = 512
ComplaintTicket = 0
ExecutionReceipt = 0
```

### 4.3 幂等

使用相同 idempotency key 并发调用创建工单 512 次：

```text
successful ToolResult = 512
unique ticket_id = 1
ComplaintTicket = 1
ExecutionReceipt = 1
```

### 4.4 暂停建立与副作用提交竞态

执行 128 轮 `request_interaction()` 与 `create_complaint_ticket()` 同时竞争。暂停检查、Ticket 写入和 Receipt 写入共享同一个 `BEGIN IMMEDIATE` 事务边界。

每一轮只允许两种结果：

```text
side effect transaction first
→ Ticket = 1
→ Receipt = 1
→ suspension established afterwards

suspension transaction first
→ create_complaint_ticket rejected
→ Ticket = 0
→ Receipt = 0
```

暂停建立后的后续副作用全部被拒绝。不存在 Ticket 已提交但 Receipt 缺失的最终状态。

### 4.5 提交后响应丢失

第一次调用完成 Ticket 和 Receipt 持久化后，模拟调用方没有收到响应；随后使用相同 idempotency key 重试：

```text
retry.ticket_id = first.ticket_id
retry.receipt_id = first.receipt_id
ComplaintTicket = 1
ExecutionReceipt = 1
```

### 4.6 多进程终结竞争

16 个使用独立进程和独立 SQLite connection 的竞争者同时调用 `finalize_execution()`：

```text
unique process ids = 16
winner = 1
AstraExecutionEnded = 1
integrity_check = ok
```

第一轮压力测试发现共享 SQLite connection 的读取并发缺陷。修复措施：

- 所有共享 connection 查询进入 `AstraStore.query_one/query_all` 锁；
- SQLite busy timeout 提升为 30 秒；
- Mock Business Service 的幂等查询和插入进入同一 `BEGIN IMMEDIATE` 事务；
- 唯一约束竞争失败后读取并返回已有业务对象和 Receipt。

## 5. Baseline 制品

固化命令：

```bash
python3 scripts/freeze_phase2_core_baseline.py
```

制品：

```text
artifacts/phase2-core-baseline-source.tar.gz
artifacts/phase2-core-baseline-manifest.json
artifacts/phase2-core-runtime-audit/
```

Manifest 记录：

- 每个 Phase 2 Core 源文件的 SHA-256；
- 确定性源码归档 SHA-256；
- Hermes 0.18.2 source manifest 和 SHA-256；
- 运行审计制品 SHA-256；
- 测试结果和压力参数；
- live Provider 未运行的原因与复验方法。

源码归档使用字典序路径、gzip/tar `mtime=0`、`uid/gid=0` 和空 `uname/gname`，可重复生成并校验。

## 6. 验证结果

```text
Project tests: 14 passed, 1 skipped
Skipped: live Provider smoke due to missing explicit credentials

Stress tests: 6 passed
Hermes compatibility regression: 157 passed, 0 failed
Ruff: All checks passed
Runtime audit: 18 checks passed
```
