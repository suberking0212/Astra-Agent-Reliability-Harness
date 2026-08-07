# ADR-003: Hermes 与 Astra 的 Task Reliability 职责边界

> **Documentation Governance**
> - **Role:** Accepted architecture decision for the Hermes/Astra reliability boundary.
> - **Authority:** A2 — Architecture / ADR.
> - **Topic:** ARCH.HERMES_ASTRA_BOUNDARY
> - **Scope:** Ownership of Agent execution reliability, Task truth, governance boundaries, and allowed defensive overlap.
> - **Not Responsible For:** Detailed field schemas, durable scheduling mechanics, implementation status, remediation, or acceptance results.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, PRODUCT.CORE
> - **Status:** FROZEN

> 决策：`ACCEPTED / PHASE 3 DESIGN FROZEN`
> 日期：2026-07-20
> 适用范围：Phase 3 及后续 Task Reliability and Governance 设计

## Context

Hermes 已经拥有完整的 Agent Execution Logic，包括模型推理、工具选择、工具结果解释、局部错误适应、Provider retry/fallback、动态重新规划、单 turn 循环保护和最终回答组织。旧 Phase 3 计划中的 Failure Detector、Recovery Engine 和部分 Execution Guard 再次实现了这些能力，形成第二套 Agent Harness，并使 Astra 的维护成本与 Hermes 内部行为绑定。

Phase 3 必须转向独立且稳定的任务级价值：持久化任务事实、生命周期权威、外部副作用责任、契约验收，以及跨 turn、attempt 和进程重启的治理。

## Decision

> **Hermes owns Agent execution reliability. Astra owns Task truth and governance.**

中文规范：

> **Hermes 负责单次 Agent 执行内部的推理、工具选择、错误适应、局部重试与循环保护；Astra 负责持久化任务事实、生命周期权威、外部副作用责任、契约验收以及跨 turn、attempt 和重启的治理。**

## Controlled extension

The narrower Learning Artifact integration boundary is defined by
[`ADR-005`](ADR-005-hermes-learning-artifact-integration.md).  ADR-005 depends
on this decision and does not transfer Hermes-owned Memory, Skill, Curator, or
Learning behavior to Astra.

## 执行边界摘要

在当前设计中，Hermes 是执行智能引擎，负责运行一次 Astra Execution 内部的 Hermes Agent Loop；Astra 是任务治理与事实系统，负责持久化状态、约束执行并判定任务结果。

核心边界是：

> **Hermes 决定如何执行；Astra 决定是否允许、是否满足任务契约，以及哪些事实可以被正式承认。**

Hermes 负责：

- 理解用户请求、Task Contract 和 Runtime Feedback；
- 调用模型推理并动态选择下一步动作；
- 选择工具、生成参数和安排工具调用顺序；
- 根据工具结果修正参数、改变方案或重新规划；
- 处理单次 Agent Loop 内的 Provider retry、backoff、fallback 和调用 timeout；
- 防止单次 Agent Loop 内的重复工具调用、局部无进展和循环失控；
- 组织最终自然语言回答；
- 保存和恢复 Hermes Session 上下文；
- 响应协作式中断；
- 通过 Observer Hooks 输出模型调用、工具调用、Agent 输出和 Session 生命周期等执行观察。

Hermes 只负责 Agent-loop-level progress。Astra 负责 Task-level progress across Executions/Attempts，包括跨 turn、Execution、Attempt 和进程重启的任务级进展判断。

业务执行边界为：

```text
Astra RuntimeInvocation
→ Hermes public plugin interface
→ Hermes Agent Loop
→ Astra Bridge Plugin
→ Astra Tool Gateway
→ Business Adapter / external system
→ submit_task_result
→ Astra Completion Validation / Policy / Runtime transition
```

Astra Bridge Plugin 只负责 Hermes 侧集成，包括注册受控工具、转发 Gateway 调用、注入已持久化的 Runtime Feedback，以及输出 Hermes 执行观察。权限、审批、幂等、ExternalOperation、Receipt 和外部副作用确认仍由 Astra Tool Gateway 与 Runtime 负责。

Observer Hooks 产生的是 Hermes 执行侧观察，不自动构成外部业务副作用或 Task 状态的权威证据。Hermes `post_tool_call` 可以证明 Hermes 收到了某个工具返回，但不能单独证明外部业务操作已经 `confirmed`。外部状态仍须通过 Tool Gateway、ExternalOperation、Receipt 和 Evidence Collection 建立权威证据。

在投诉处理场景中，Hermes 可以自主决定查询客户和订单、检索售后政策、选择解决方案、创建投诉工单、回读业务状态、提交结果和生成用户回答。具体执行顺序不是 Astra 写死的 Workflow，而由 Hermes 根据上下文动态决定。

当信息不足或操作需要审批时，Hermes 可以调用 `request_user_input` 或 `request_approval`。Astra Runtime 会持久化 Interaction，终结当前 Execution，并将 Task 和 Attempt 转入等待状态。Interaction 解决后，Runtime 创建新的 Astra Execution，并根据 Session policy 复用或重建 Hermes Session，再启动新的 Hermes turn，而不是恢复原来的 Python 调用栈。

Hermes 不是 Task 的最终权威：

- Hermes `completed=true` 只表示本轮 Agent 执行正常返回；
- Hermes 不决定 Task、Attempt 或 Execution 的权威生命周期状态；
- Hermes 不负责 Task-level exactly-once、业务幂等、外部副作用责任或最终对账；
- Hermes 不得绕过 Astra 的 Tool whitelist、Schema、权限、审批和 budget；
- Hermes 的 Tool Result 和 Observer Hook 只能证明执行侧观察，不能单独证明外部业务状态已经确认；
- Hermes 无权将 Task 正式标记为成功。

`submit_task_result` 只提交并持久化候选任务结果，不等同于完成决策。Phase 3 的正式语义是：

```text
persist result submission
→ collect/fix EvidenceSnapshot
→ evaluate Completion Contract
→ apply Task Policy
→ Runtime completion gate
→ CAS transition
```

即使 Hermes 输出“投诉已经创建”，如果缺少有效 Receipt、ExternalOperation confirmation 或 Completion Contract 所要求的其他证据，Astra 也只能记录 `task_outcome_not_validated` 验证结果或 Execution termination reason。随后由 Task Policy 决定继续执行、请求输入、请求审批、进行对账、开启新 Attempt 或失败，而不能直接将 Task 标记为成功。

当前实现状态必须区分：

- Phase 2 已完成 Hermes 接入和确定性闭环验证；
- Phase 3 的契约领域层与 Runtime Governance Core 已由现有
  `Phase2Runtime → RuntimeGovernanceCore → AstraStore` 链实现并冻结；
- 完整 Task Runtime、checkpoint restart recovery、queue/scheduling 和
  multi-instance coordination 属于 Phase 4，不在 Phase 3 中扩展。

### Hermes owns

- 模型推理、上下文解释和下一步动作选择；
- Tool selection、argument generation、execution order；
- Tool Result 解释、参数修复和局部错误适应；
- Provider retry、backoff 和 fallback；
- 单次调用 timeout 处理；
- 单次 Agent Loop 内的重复工具调用、局部无进展和循环失控保护；
- Agent 内部动态 replanning；
- 最终自然语言回答组织。

Astra 不实现 `retry_same_tool`、`repair_arguments`、`choose_alternative_tool`、固定工具顺序或业务计划。

Astra 也不调度、配置或调用 Hermes 的 Curator、Memory、Skill 或其维护生命周期，
不实现 Provider/credential 产品层，也不得通过 monkey-patch 或其他私有 Hermes
runtime 接口改变这些能力。Astra 可为隔离和评测创建独立 `HERMES_HOME`，并通过
公开集成边界记录观察；这不转移 Hermes 的行为、记忆、学习或维护所有权。

### Astra owns

- Task、Attempt、Execution 的持久化身份和状态转换权；
- Task Contract 和 Completion Contract；
- Result submission、Requirement Evaluation 和最终 Task completion gate；
- Receipt、外部副作用操作记录、幂等和 exactly-once 本地终结；
- 权限、审批、业务约束和 Interaction 持久化；
- 跨 Execution/Attempt 的任务级进展判断；
- 外部业务状态采集、对账和不确定状态管理；
- Task/Attempt budget、deadline、retry 上限；
- 可审计 PolicyDecision 和 Task 终态。

### 允许的防御性重叠

Astra 可以在执行边界强制保护自身或业务系统拥有的权威，包括 Tool whitelist、Schema、权限、审批、幂等、Receipt、Task budget 和 exactly-once。该类约束回答“是否允许”，不能替 Hermes 决定正常业务动作。

一项能力只有至少满足以下一个条件，才允许进入 Astra Core：

- 必须跨 turn、attempt 或进程重启；
- 涉及外部业务副作用或业务系统权威；
- 决定 Task 状态或契约完成；
- 属于权限、审批、合规、幂等或审计；
- 必须独立于 Hermes、Provider 和 Tool 实现保持稳定。

## Authoritative truth

Authoritative Record 决定当前状态：

- Task / Attempt / Execution row；
- Receipt；
- Interaction；
- External Operation record；
- 外部 Business Entity 或其固定、带版本 Evidence Snapshot。

Reliability Fact 是不可变的治理证据索引，不是第二套权威数据库。Policy 和 completion gate 必须读取权威快照，不能通过 Fact 回放推断当前 Task 状态。

## Decision architecture

```text
Authoritative Records
→ atomic Fact/Outbox or Evidence Collection
→ Authoritative Evidence Snapshot + supporting Facts
→ Task Rules / Requirement Evaluators
→ DecisionContext
→ Task Policy
→ PolicyDecision
→ Runtime CAS + atomic transition
```

Phase 3 采用少量固定 Decision Point，不建设通用 CEP、规则 DSL、复杂事件总线、分布式流处理、动态策略语言或 Workflow Engine。

## Consequences

- Hermes 升级引起的错误码、Provider 和 Tool 行为变化应被限制在 Adapter 和 Fact attributes；
- Astra Core 不依赖 Hermes 私有类型或固定错误字符串；
- Normal completion、Runtime invariant、Task Rule 和 Policy 的职责必须分离；
- External side effect 无法确认时必须表达为 `indeterminate`，不得伪造 exactly-once；
- Phase 3 实现以已确定的契约文档为边界。

## Normative references

- `docs/phase3/TASK_CONTRACT.md`
- `docs/phase3/EFFECT_IDENTITY_AND_IDEMPOTENCY.md`
- `docs/phase3/APPROVAL_BINDING.md`
- `docs/phase3/TASK_LIFECYCLE.md`
- `docs/phase3/RELIABILITY_FACTS_AND_EVIDENCE.md`
- `docs/phase3/TASK_RULES.md`
- `docs/phase3/TASK_POLICY.md`
- `docs/phase3/REQUIREMENT_EVALUATORS.md`
- `docs/phase3/DECISION_POINTS_AND_SNAPSHOTS.md`
