# Astra × Hermes 阶段记录

> 更新日期：2026-07-20  
> 当前状态：`Phase 1 CLOSED` / `Phase 2 CORE CLOSED AND BASELINED` / `PHASE 2 LIVE VALIDATION PENDING: MISSING CREDENTIALS` / `PHASE 3 DESIGN FROZEN` / `PHASE 3 CONTRACT DOMAIN LAYER IMPLEMENTED` / `PHASE 3 RUNTIME GOVERNANCE CORE COMPOSED INTO PHASE 2 RUNTIME`

## 1. Phase 1 正式关闭

Phase 1 is closed.

Hermes 0.18.2 的主执行链、核心模块职责、可复用能力、任务级可靠性缺口，以及 Observation、Constraint、Feedback 三类稳定接入边界，已经完成源码与测试验证。

现有兼容性测试已经证明，在不修改 Hermes Agent Loop 的情况下，Astra 可以通过 Plugin、Observer、Middleware、Tool Hooks 和程序化 `AIAgent` 入口，实现受控工具调用、调用前约束、结构化反馈回灌、下一轮推理以及 Session 持久化。

源码快照身份已经固定在 `artifacts/hermes-source-manifest.json`：

| 字段 | 值 |
| --- | --- |
| Repository | `NousResearch/hermes-agent` |
| Declared version | `0.18.2` |
| Commit SHA | `null` |
| Archive SHA-256 | `200a11f1bfe275b77cf69882ed603cb1e0144153de8524b2283310d981529e3b` |
| Source tree SHA-256 | `95239075bca612240d6f76ceae9b3d2d07ea7209de671641f2e89c5e09d4426e` |
| Python | `3.13.11` |
| Compatibility tests | `158 passed, 0 failed` |

完整 Git 仓库恢复、候选 commit 比对和 commit SHA 深度追溯是后续独立任务，不阻塞 Phase 1。真实 Provider E2E 是 Phase 2 的 opt-in 集成验证，也不阻塞 Phase 1。

## 2. 未完成事项的正式分类

| 项目 | 正确性质 | 阶段处理 |
| --- | --- | --- |
| 缺少 commit SHA | 来源可追溯性缺口 | Deferred，不阻塞 Phase 1 |
| 缺少真实 Provider E2E | 验证覆盖缺口 | Phase 2 opt-in smoke test |
| 没有通用 pre-final gate | Hermes 0.18.2 已确认的能力限制 | `GenericFinalGateShim` 延后 |
| 没有正式 Token/Cost pre-call deny | Hermes 0.18.2 已确认的能力限制 | `PreProviderBudgetShim` 延后 |
| 部分路径可能绕过 finalizer | Adapter 必须吸收的兼容性风险 | Phase 2 exactly-once termination |
| 不能恢复 Python 调用栈 | Astra 已接受的正式运行模型 | 不再作为缺陷 |

Astra 的正式恢复模型是：

> Astra 不恢复进程内 Python 调用栈，而是持久化 Task、InteractionRequest、ExecutionReceipt 和 Hermes Session。任务恢复时，通过新的 Hermes turn 继续执行。

## 3. Phase 2 首要目标

Phase 2 只先实现一个可运行、可验证的正常业务纵向闭环：

```text
RuntimeInvocation
→ Hermes Executor Adapter
→ Hermes Bridge Plugin
→ Astra Tool Gateway
→ Mock Business Service
→ submit_task_result
→ ResultReceipt
→ ExecutionResult
→ Neutral Trace
```

首条业务场景为正常投诉处理：查询订单、查询政策、创建投诉工单、提交处理结果。工具选择和顺序由 Hermes 根据上下文自主决定，不编码为固定 Workflow、DAG 或工具链。

Phase 2 首批必须交付：

1. 不含 Hermes 私有类型的 Astra 领域接口：`AgentExecutor`、`RuntimeInvocation`、`ExecutionResult`、`ExecutionEvent`、`ToolInvocationContext`、`ToolResult`；
2. Hermes Executor Adapter；
3. 固定 Hermes Bridge Plugin；
4. 只含白名单、Pydantic 校验、Mock Adapter 路由、结构化错误、最小回执和暂停保护的 Tool Gateway；
5. 正常投诉业务场景与 Mock Business Services；
6. `submit_task_result` 最小验证闭环；
7. 数据库约束支持的 exactly-once Execution 终结；
8. Runtime Interaction Primitive 契约；
9. 最小 Budget Ledger；
10. 只观察、不干预的 Neutral Trace；
11. 默认关闭的 live Provider smoke test。

## 4. `submit_task_result` 权威边界

模型可调用的接口固定为：

```python
submit_task_result(
    outcome,
    evidence_refs,
    receipt_refs,
)
```

模型只能提交结果声明、证据索引和回执索引，不能提交、修改或重新解释 Completion Contract。Completion Contract 必须来自 Astra 持久化的 Task Contract。

最小验证流程：

```text
模型提交 outcome / evidence_refs / receipt_refs
→ 系统读取持久化 Completion Contract
→ 系统读取实际业务状态和 ExecutionReceipt
→ 验证结果声明及引用
→ 生成 valid 或 invalid ResultReceipt
→ 设置 task_outcome_validated
```

最小验证器不直接把 Task 标记为 `succeeded`，也不等同于 Phase 3 Requirement Evaluators、Completion Aggregator 和 Runtime completion gate。

## 5. 结果、Turn 与 Task 状态分离

以下三个概念必须独立记录：

```text
task_outcome_validated
agent_turn_finished
task.status
```

Task 进入 `succeeded` 的必要条件为：

```text
task_outcome_validated = true
+ no unresolved interaction
+ required business state reconciled
+ runtime termination policy satisfied
→ Task may enter succeeded
```

最终状态转换只能由 Astra Task Runtime 执行。Hermes 的 `completed`、单次结果验证回执以及 Adapter 的 Execution 终结，都不能单独决定 Task 成功。

业务完成事实也不依赖 Hermes 后续自然语言说明成功生成。如果 ResultReceipt 有效、业务状态已对账、无未解决 Interaction 且满足 Runtime 终结策略，即使最终说明因网络错误缺失，Runtime 仍可按策略成功终结 Task，并记录：

```text
task_outcome_validated = true
agent_turn_finished = false
user_explanation_status = missing_or_fallback
```

系统可以用模板化摘要作为降级输出。

## 6. Exactly-once Execution 终结

Execution 的正式终结必须由内存状态机和持久化原子约束共同保证，不能只依赖进程内锁或本地 compare-and-set。

可采用事件唯一约束：

```text
UNIQUE(execution_id, event_type = AstraExecutionEnded)
```

或一次性条件更新：

```sql
UPDATE execution
SET status = :status,
    ended_at = :ended_at
WHERE execution_id = :id
  AND ended_at IS NULL;
```

只有真正插入唯一终结事件或更新一行的竞争方，才有资格发布正式 `AstraExecutionEnded` 并推进 Runtime。其他调用方只能识别“已经结束”。该约束必须覆盖正常返回、异常、cancel、timeout、Hook/Adapter 重复结束、重启补偿、重复消费，以及 Hermes 绕过正常 finalizer 的路径。

Execution 结束不等于 Task 成功。Execution 结束后，Task 仍可能等待输入、等待审批、等待业务对账或等待 Runtime 终结判定。

## 7. 交互暂停不变量

第一版定义：

```text
request_user_input
request_approval
InteractionRequest
suspension_requested
WAITING_INPUT
WAITING_APPROVAL
interaction resolution / resume entry
```

具体 Hook 与 interrupt 顺序必须根据 Hermes 0.18.2 的实际时序实现，不预先绑定“先返回 Tool Result”或“先 interrupt”。但以下不变量不可破坏：

```text
一旦 suspension_requested = true
→ 不允许新的业务副作用
→ 必须持久化 InteractionRequest
→ 当前 execution 必须收敛为 WAITING_INPUT 或 WAITING_APPROVAL
```

Tool Gateway 或受控业务工具层必须在暂停请求后拒绝新的副作用。等待状态建立后 Hermes 不能再执行写操作。恢复时读取持久化状态和 Hermes Session，启动新的 Hermes turn；Interaction 解决前 Task 不得进入 `succeeded`。

## 8. Phase 2 最小 Budget Ledger

首版只记录：

```text
provider_request_count
observed_input_tokens
observed_output_tokens
observed_total_tokens
estimated_cost_usd
reserved_next_call_tokens
```

仅支持：

- `observe_only`：记录但不阻断 Provider 调用；
- `conservative_limit`：基于保守估算，在明显可能超限时限制后续调用。

它提供透明、可解释、保守的资源限制，但不宣称是精确财务硬预算。Phase 2 初期不建设历史价格数据库、价格版本、汇率、账单对账、完整 Provider Proxy 或 `PreProviderBudgetShim`。

## 9. Neutral Trace 与 Live Provider 验证

Neutral Trace 第一版只记录：Execution start、Agent step、Provider call、Tool call、Tool result、Interaction request、Result submission、Result validation、Execution end。

Neutral Trace 不参与执行决策、工具阻断、自动恢复或 Task 成功判定。

Live Provider smoke test：

- 默认不运行；
- 不阻塞普通 CI；
- 需要显式 Provider 凭证；
- 验证真实模型、真实 Hermes Loop、受控工具、正常投诉链路、ResultReceipt、ExecutionResult 和 Trace 完整性。

## 10. Phase 2 初期明确排除

```text
GenericFinalGateShim
PreProviderBudgetShim
完整 Task Runtime
Checkpoint restart recovery
Task-level Rules
Task Policy
完整 Requirement Evaluators / Completion Aggregator
完整审批 UI
前端系统
A/B/C 大规模评估
完整 Provider Proxy
精确财务预算系统
```

Phase 2 仍会实现最小状态转换、最小正常结果验证、最小交互暂停契约和最小预算账本，但这些不等于完整可靠性平台。

## 11. Phase 2 实施结果

Phase 2 最小纵向闭环已实现，代码位于：

```text
astra/domain.py
astra/storage.py
astra/runtime.py
astra/budget.py
astra/trace.py
astra/mock_business.py
astra/tool_gateway.py
astra/result_validator.py
astra/hermes_adapter/
.hermes/plugins/astra_bridge/
```

已实现能力：

1. Hermes-independent `AgentExecutor`、`RuntimeInvocation`、`ExecutionResult`、`ExecutionEvent`、`ToolInvocationContext` 和 `ToolResult`；
2. 真实 Hermes `AIAgent` Executor Adapter；
3. 固定 `astra_bridge` Project Plugin，并按 Task Contract 只暴露允许的业务 Toolset；
4. Pydantic Schema、白名单、权限、暂停保护、SQLite Receipt 和幂等约束组成的最小 Tool Gateway；
5. SQLite-backed Customer、Order、Policy、ComplaintTicket Mock Business Services；
6. 正常投诉处理脚本化 Provider E2E，工具选择发生在真实 Hermes Loop 内；
7. `submit_task_result`、实际业务状态对账和 valid/invalid ResultReceipt；
8. `InteractionRequest` 持久化、`suspension_requested` 副作用阻断和基于 opaque Session 的新 turn 恢复入口；
9. `ended_at IS NULL` 条件更新与唯一 `AstraExecutionEnded` 事件共同提供的 exactly-once 终结；
10. `observe_only` / `conservative_limit` Budget Ledger；
11. 只观察、不干预的 Neutral Trace；
12. 默认 skip、需要显式凭证的 live Provider smoke test。

验证结果（2026-07-19）：

```text
pytest -q
14 passed, 1 skipped

Skipped:
tests/test_phase2_live_provider.py
原因：未提供 ASTRA_RUN_LIVE_PROVIDER=1 和显式 Provider 凭证

Hermes compatibility regression:
157 passed, 0 failed

Ruff:
All checks passed
```

新增审计与压力验证结果：

```text
Runtime persistence audit:
18 checks passed
25 Neutral Trace spans
3 ExecutionReceipt
1 valid ResultReceipt
1 persisted complaint ticket
1 Hermes Session / 10 persisted messages
SQLite integrity_check = ok
foreign_key_check = clean

Stress verification:
512 concurrent termination attempts across independent SQLite connections
→ exactly 1 winner / 1 AstraExecutionEnded

512 side-effect attempts after suspension_requested
→ 512 rejected / 0 ticket / 0 receipt

512 concurrent calls with one idempotency key
→ 1 ticket / 1 receipt

128 request_interaction vs create_complaint_ticket races
→ each race resolves to exactly one atomic ordering
→ committed side effect always has Ticket + Receipt
→ pause-first outcome has 0 Ticket + 0 Receipt
→ every later side effect is rejected

Committed Ticket + Receipt followed by simulated response loss
→ retry with the same idempotency key returns the same ticket_id and receipt_id
→ database remains at 1 Ticket + 1 Receipt

16 independent processes finalize the same Execution
→ exactly 1 winner / 1 AstraExecutionEnded
→ SQLite integrity_check = ok
```

第一轮压力测试发现共享 SQLite connection 的只读查询没有统一进入存储层锁，可能在并发暂停检查和幂等写入之间产生 `sqlite3.InterfaceError` 或干扰唯一约束异常处理。该问题已通过 `AstraStore.query_one/query_all` 收口共享连接读取，并将 Mock Business Service 的幂等检查与插入合并到同一原子事务中修复。修复后压力测试和全量回归均通过。

Phase 2 Core 的并发与持久化基础安全性已经达到关闭门槛，并固化为：

```text
artifacts/phase2-core-runtime-audit/
artifacts/phase2-core-baseline-source.tar.gz
artifacts/phase2-core-baseline-manifest.json
docs/PHASE2_CORE_BASELINE.md
```

真实 Provider 网络请求仍未执行，因为当前主机未发现 `ASTRA_LIVE_*`、OpenAI、Anthropic、OpenRouter、Nous、Gemini 或 Hermes Provider 凭证/配置。该阻塞不影响 `Phase 2 Core: CLOSED AND BASELINED`，但独立的 `Phase 2 Live Validation` 保持 `PENDING`。

## 12. 后续延后项

```text
Deferred
├── commit SHA 深度追溯
├── GenericFinalGateShim
├── PreProviderBudgetShim
├── Checkpoint restart recovery
├── 完整调度、队列和分布式 Task Runtime
├── 完整审批 UI
├── 前端
└── 大规模可靠性评估
```

最终阶段原则：Phase 1 证明 Hermes 如何接入、稳定边界在哪里、已有能力和版本限制是什么，以及 Astra 必须补齐什么；Phase 2 基于这些结论，实现第一个可运行、可验证的业务纵向闭环。

## 13. Phase 3 重新设计状态

旧 Phase 3 中以 `Failure Detector`、`Recovery Engine`、执行期错误分类和 Tool retry 动作为中心的设计不再作为实施依据。该方向与 Hermes 已拥有的 Agent execution reliability 重叠，容易形成第二套 Agent Harness。

新的正式职责边界为：

> **Hermes owns Agent execution reliability. Astra owns Task truth and governance.**

中文规范：

> **Hermes 负责单次 Agent 执行内部的推理、工具选择、错误适应、局部重试与循环保护；Astra 负责持久化任务事实、生命周期权威、外部副作用责任、契约验收以及跨 turn、attempt 和重启的治理。**

Phase 3 采用契约优先、决策点驱动的 Task Reliability and Governance 设计：

```text
Authoritative Records
→ atomic Fact/Outbox or Evidence Collection
→ Authoritative Evidence Snapshot + supporting Reliability Facts
→ Task Rules / Requirement Evaluators
→ DecisionContext
→ Task Policy
→ PolicyDecision
→ Runtime CAS + atomic transition
```

Reliability Fact 不是第二套权威数据库。Task、Attempt、Execution、Receipt、Interaction、ExternalOperation 和外部业务实体仍是 Authoritative Record；涉及当前状态的最终判断必须读取带版本的权威 Snapshot。Evaluator 只读取固定 EvidenceSnapshot，不允许边读取业务系统边写 Fact。

Phase 3 设计权威文档：

```text
docs/adr/ADR-003-task-reliability-boundary.md
docs/phase3/TASK_CONTRACT.md
docs/phase3/EFFECT_IDENTITY_AND_IDEMPOTENCY.md
docs/phase3/APPROVAL_BINDING.md
docs/phase3/TASK_LIFECYCLE.md
docs/phase3/RELIABILITY_FACTS_AND_EVIDENCE.md
docs/phase3/TASK_RULES.md
docs/phase3/TASK_POLICY.md
docs/phase3/REQUIREMENT_EVALUATORS.md
docs/phase3/DECISION_POINTS_AND_SNAPSHOTS.md
```

冻结清单：

```text
[x] Boundary ADR approved
[x] Normalized Task Contract frozen
[x] Canonical effect identity and idempotency frozen
[x] Exact Approval binding frozen
[x] Task/Attempt/Execution lifecycle and bounds frozen
[x] Authoritative Record / Fact / Outbox authority model frozen
[x] ExternalOperation crash-window semantics frozen
[x] Evidence Collection and immutable EvidenceSnapshot frozen
[x] Task Rule responsibility frozen
[x] Task Policy actions and feedback constraints frozen
[x] Requirement Evaluator and Aggregator contract frozen
[x] Decision Point, idempotency key and CAS contract frozen
[x] Contract tests approved
```

Round 2 完成后正式状态：

```text
Phase 3 Design: FROZEN
Phase 3 Implementation: NOT STARTED
```

后续实现必须遵守 frozen contracts。任何 Core Contract 变化都需要新的 ADR/contract version 和七场景同等级回归，不得作为普通实现细节静默改变。

## 14. Phase 3 Contract Scenario Validation — Round 1

2026-07-20 已完成第一轮场景驱动契约验证，覆盖：客服投诉、退款审批、创建 Jira 工单、CRM 更新客户信息、采购审批、邮件发送和 Browser Agent 自动填表。

正式报告：`docs/phase3/CONTRACT_SCENARIO_VALIDATION.md`。

结论：Completion/Evidence/Requirement/Policy/Runtime 的通用职责链可以覆盖这些业务，不需要新增 Workflow、DAG、业务专用 Core Policy action 或生命周期状态；业务差异可以保留在 Plugin/MCP、Tool Adapter、Evidence Collector 和 versioned Requirement Evaluator。

本轮发现三个设计冻结阻塞项：

```text
1. 缺少独立、规范化、可版本化的 Task Contract schema；
2. approval requirement/token 尚未与精确 operation subject/request/effect identity 形成规范绑定；
3. ExternalOperation、idempotency、duplicate-side-effect Rule 和 approval 共用的 canonical effect identity 尚未定义。
```

Round 1 当时状态为：

```text
Phase 3 Contract Validation Round 1: COMPLETED
Phase 3 Design: CORE CHANGES REQUIRED / NOT FROZEN
Phase 3 Implementation: NOT STARTED
```

## 15. Phase 3 Contract Scenario Validation — Round 2 and Design Freeze

2026-07-20 已按依赖顺序补齐并冻结：

```text
Task Contract
→ Canonical Effect Identity and Idempotency
→ Exact Approval Binding
```

随后将七个典型业务转换为 table-driven fixtures，每个场景覆盖 success、missing input、approval required、response lost/indeterminate 和 stale decision，共 35 条纯契约链。

正式报告：`docs/phase3/CONTRACT_SCENARIO_VALIDATION_ROUND2.md`。

验证结果：

```text
Round 2 contract tests: 50 passed
Full regression: 64 passed, 1 skipped
Ruff: All checks passed
Core Task states: unchanged
Core Policy actions: unchanged
Completion Aggregator: unchanged
Runtime CAS model: unchanged
```

唯一 skip 仍为缺少显式凭证的 opt-in live Provider smoke test，不影响设计冻结。

最终决定：

```text
Phase 3 Design: FROZEN
Phase 3 Implementation: NOT STARTED
```

## 16. Phase 3 Contract Domain Layer Implementation

2026-07-20 已按冻结契约实现第一批生产领域层，代码位于
`astra/phase3/`。本批实现严格限定为领域模型、值对象、版本化注册表和纯函数服务：

```text
Task Contract validation and canonical hash
→ CanonicalEffectRequest and effect_identity
→ exact Approval binding
→ ExternalOperation / idempotency value objects
→ immutable EvidenceSnapshot
→ RequirementEvaluation / Completion Aggregator
→ PolicyDecision
→ pure CAS and completion-gate verdict
```

本批没有新增平行 Runtime、Engine、Coordinator、Dispatcher、Workflow 或
Orchestrator；`RuntimeGovernanceCore` 作为现有 `Phase2Runtime` 的可调用领域组件
复用同一 `AstraStore` 连接与事务。它不修改 Hermes Tool 选择、参数生成、执行顺序或局部重试职责；
也没有把任何投诉、退款、Jira、CRM、采购、邮件或 Browser 业务字段写入 Core。
Phase 3 以 `python -m astra.phase3` / `astra-phase3` CLI 为唯一开发入口；
Web API 与 WebUI 不在 Phase 3 范围内。

验证结果：

```text
Round 2 frozen contract tests: 50 passed
Production domain/governance Round 2 tests: 40 passed
Full regression: 104 passed, 1 skipped
Ruff (astra + tests): All checks passed
```

当前实施状态：

```text
Phase 3 Contract Domain Layer: IMPLEMENTED
Phase 3 Runtime Governance Core: IMPLEMENTED AS PHASE 2 RUNTIME COMPONENT
Parallel Phase 3 Runtime / scheduler / workflow layer: NOT CREATED
```

## 17. Phase 3 Formal Completion and Freeze Baseline

2026-07-20 已完成 Phase 3 正式验收与可复现基线冻结。冻结实现继续由
`Phase2Runtime → RuntimeGovernanceCore → AstraStore` 承载，不新增第二套
Runtime，也不改变 Hermes/Astra 职责边界、Task Contract、领域模型或事务边界。

正式冻结基线：

```text
validate-round2: 35/35 passed
validate-governance-round2: 35/35 passed
pytest: 104 passed, 1 skipped
ruff: All checks passed
```

唯一 skipped 项是 opt-in live Provider smoke test，分类为：

```text
environment-dependent non-blocking test
```

该测试依赖外部凭证和网络环境，不属于 Phase 3 核心 Gate，不影响 Phase 3
验收。不得为了消除 skip 将外部凭证或网络依赖引入核心 Gate。

验收输出归档于 `artifacts/phase3-governance-complete/`，Git 标签为
`phase3-governance-complete`。自该基线起，Phase 3 进入冻结状态；后续实现
修改必须由新的 Phase 4 验收项驱动，不得因代码优化、架构调整或个人偏好
继续修改已冻结的 Phase 3 实现。

```text
Phase 3 Acceptance: COMPLETE
Phase 3 Implementation: FROZEN
Phase 3 Tag: phase3-governance-complete
```
