# Astra Product Evolution Roadmap（产品演进路线图）

> **Documentation Governance**
> - **Role:** Informational product-evolution roadmap and document-retirement review.
> - **Authority:** Informational — separates current Product commitments from future hypotheses without changing Product scope.
> - **Scope:** Orders the proposed evolution from a reliable single Hermes Agent toward delegation, Multi-Agent, Crew, and long-running Employee concepts, and records documentation cleanup recommendations.
> - **Not Responsible For:** Authorizing future scope, defining architecture or Runtime contracts, committing dates, assigning release status, or superseding registered Topic Authorities.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, PRODUCT.CORE, ARCH.HERMES_ASTRA_BOUNDARY, ARCH.PERSISTENT_TASK_RUNTIME
> - **Status:** HISTORICAL REFERENCE — future hypotheses only; not implementation authority.

本文回答两个问题：

1. Astra 应该按照什么顺序，从当前 Single Agent Runtime 演进到更复杂的 Agent
   系统？
2. 随着这条主线变清晰，哪些旧文档必须保留，哪些可以归档，哪些未来可以删除？

本文不是新的 PRD。当前权威产品范围仍由 [`PRD.md`](../PRD.md) 定义。该 PRD 明确
将 Multi-Agent coordination、organization system 和 employee registry 列为当前
Out of Scope，因此本文中的 Delegation、Multi-Agent、Crew 和 Employee 都只是
未来产品假设，不是当前开发承诺。

## 1. 一句话路线

```text
Single Hermes ReAct MVP
→ Reliable Single Agent
→ 多业务验证与可靠性评估
→ Delegation / Child Task
→ Multi-Agent Runtime
→ Crew
→ Long-running Employee
```

这条路线的原则不是“不断增加 Agent 数量”，而是逐步扩大 Astra 对工作的承载
范围：

```text
一次执行
→ 一项可恢复的任务
→ 多项可验证的业务任务
→ 可委派的任务树
→ 多执行者协作
→ 稳定团队
→ 长期岗位责任
```

## 2. 路线状态的含义

| 标记 | 含义 |
|---|---|
| Current Commitment | 已存在于当前 `PRODUCT.CORE` 范围，可以继续设计、实现和验收 |
| Decision Candidate | 在当前产品证据完成后值得评审，但尚未获得产品扩域授权 |
| Future Hypothesis | 用于保持方向感；不得驱动当前代码提前抽象或扩大范围 |

路线图中的先后顺序表示依赖关系，不表示发布日期。

## 3. M0：Single Hermes ReAct MVP

**状态：Current Commitment**

目标是交付一条用户能够理解和运行的真实业务闭环：

```text
CLI submit complaint
→ Runtime 创建 Task / Attempt / Run Request
→ Worker 创建 Execution
→ Hermes 执行一次 ReAct loop
→ Gateway 调用 Business Sandbox
→ 创建真实 complaint ticket
→ Hermes submit_task_result
→ Governance 验收
→ Runtime succeeded
→ CLI status 返回业务对象和完成原因
```

最小形态是：

```text
1 Task
1 Attempt
1 Run Request
1 Execution
1 Hermes ReAct loop
1 governed business effect
1 final PolicyDecision
```

### Exit Gate

- 一个不了解内部代码的人可以只通过正式入口运行 complaint Golden Path；
- Business Sandbox 中存在唯一真实 ticket；
- Task 成功来自证据和 Governance，而非 Hermes 自述；
- `status` 能解释执行、业务结果与完成原因；
- Walkthrough 与实际代码、命令和黑盒测试一致。

当前启动与公开接口边界见 [`README.md`](../../README.md)。

## 4. M1：Reliable Single Agent

**状态：Current Commitment**

这一阶段不是再造 Hermes retry，而是让一个 Task 在多次 Execution、Attempt 和进程
重启之间仍能安全继续。

### 4.1 三层重试边界

```text
Hermes-local retry
  Provider retry / fallback
  Tool 参数修正
  单次 Agent Loop 内 replanning

New Execution, same Attempt
  waiting_input resume
  waiting_approval resume
  continue_with_feedback
  安全的 durable continuation

New Attempt
  上一 Attempt 已正式结束
  Task retry budget 仍允许
  Governance 明确选择 start_new_attempt
```

Astra 不实现 `retry_same_tool`、`repair_arguments` 或固定业务步骤；这些属于 Hermes
Agent execution logic。Astra 负责 Task-level limits、外部副作用不确定性、Attempt
转换、恢复、取消和最终状态。

### Exit Gate

同一个真实 complaint 业务必须证明：

- waiting input 后在同一 Attempt 创建新 Execution 并成功；
- approval 后精确绑定 effect，恢复后只产生一个业务副作用；
- cancel 成为权威后，迟到 Worker 不能创建副作用或覆盖终态；
- Runtime 重启后能继续或 reconciliation，而不是盲目重放；
- `start_new_attempt` 原子结束旧 Attempt 并建立新 Attempt；
- retry、deadline 和 budget exhaustion 都产生可解释结果。

## 5. M2：多业务 Single Agent 验证

**状态：Current Commitment 的验证扩展**

在引入 Multi-Agent 前，必须先证明 Runtime 不是只为 complaint ticket 硬编码。
根据当前 PRD 的 Business Environment 范围，可逐步加入有限、可确定性验证的业务
场景，例如：

```text
投诉工单
退款审核
替换申请
通知或跟进
订单异常调查
```

每个场景仍然采用：

```text
一个 Task
→ 一个 Hermes Agent 自主选择工具
→ 一个或多个受治理业务结果
→ 独立证据验收
```

### Exit Gate

- 至少两个不同业务闭环复用同一 Runtime 生命周期；
- 新业务只通过 Contract、工具/业务 Adapter 和 Evaluator 等明确扩展点接入；
- Runtime、Attempt、Execution、Run Request 不包含 complaint 专属语义；
- 不通过固定 Workflow 或 Tool 顺序实现业务；
- 相同的等待、取消、恢复和审计能力适用于不同业务。

## 6. M3：可靠性评估与产品演示

**状态：Current Commitment，但仍有治理缺口**

当前 PRD 要求比较：

```text
A. Hermes Baseline
B. Hermes + bounded basic retry
C. Hermes + Astra reliability capabilities
```

三组必须共享相同任务、业务初态、工具接口和独立 Evaluation Oracle，不能把 C 组
自己的 Task success 当作实验真值。

同时需要把产品闭环做成可理解的演示：

- 正常完成；
- waiting/resume；
- approval；
- transient failure 与恢复；
- false completion 被拒绝；
- cancel fencing；
- restart/reconciliation；
- 最终业务对象、证据和决策解释。

### Exit Gate

- Evaluation Method Topic 和对应 Engineering Acceptance 已注册；
- 评估集、故障注入、Rubric 和 Oracle 有版本；
- Baseline/Retry/Astra 的开关边界可复现；
- 演示不依赖直接修改数据库或测试私有入口；
- 产品声明不超过真实评估与 live validation 证据。

## 7. G1：是否扩大为 Multi-Agent 产品

**状态：Decision Candidate**

M0–M3 完成后，必须先做产品决策，而不是直接开始写 Multi-Agent 代码。

只有同时满足以下条件，才建议修改 `PRODUCT.CORE`：

1. 真实任务存在单 Agent 明确无法有效承担的可分离子问题；
2. 多 Agent 能引入新的工具、环境反馈、权限或独立验证，而不只是增加讨论轮次；
3. 收益足以覆盖额外 token、延迟、并发、冲突和审计成本；
4. Parent/Child Task 的完成、取消、预算和证据传播可以被清楚定义；
5. 现有 Single Agent Runtime 已经稳定，复杂度不是用 Multi-Agent 掩盖基础问题；
6. 至少一个真实业务场景需要 Delegation，而不是为了展示架构概念。

如果不满足这些条件，产品应继续优化 Reliable Single Agent，而不是进入 M4。

### 扩域所需治理动作

进入 M4 前至少需要：

```text
修改 PRODUCT.CORE
→ 新建并接受 Multi-Agent / Delegation 架构 ADR
→ 在 AUTHORITY 注册新的 Runtime Design Topic
→ 定义 Engineering Acceptance
→ 才能开始正式实现
```

路线图本身不能代替这些动作。

## 8. M4：Delegation 与 Child Task

**状态：Future Hypothesis**

真正的 Multi-Agent 地基不是“多个 Agent 互相聊天”，而是可治理的任务委派：

```text
Parent Task
├── Child Task A
├── Child Task B
└── Child Task C
```

父 Agent 只提出委派请求，Astra Runtime 负责建立 Child Task，并为每个 Child Task
提供完整的 Contract、权限、预算、生命周期和 Governance。

必须解决：

- Parent/Child identity；
- Delegation Contract；
- 子任务输入快照与完成条件；
- 权限与业务副作用隔离；
- 父预算与子预算；
- dependency、cancel 和 failure propagation；
- governed result handoff；
- 子任务证据如何成为父任务输入；
- 父任务如何处理 child waiting、failed 或 indeterminate。

核心不变量是：

> Parent Agent 不能仅因为 Child Agent 声称完成，就把子任务结果当作权威事实。

### Exit Gate

一个父 Task 可以创建一个 Child Task；Child Task 经自己的 Runtime 和 Governance
完成后，以结构化、可追溯的 Handoff 回到父 Task；父 Task 再创建新 Execution 并
继续，且取消、预算和副作用身份没有歧义。

## 9. M5：Multi-Agent Runtime

**状态：Future Hypothesis**

在 Child Task 成熟后，再支持多个独立 Agent 串行或并行工作：

```text
Coordinator
├── Investigator
├── Policy Specialist
├── Action Agent
└── Reviewer
```

这一阶段重点不是角色 Prompt，而是 Runtime coordination：

- Agent identity 与 Executor selection；
- 独立 context 和 tool/permission boundary；
- 并行 Child Task 调度；
- 结构化 message 与 artifact handoff；
- 总预算、子预算和 deadline；
- 结果冲突和独立验证；
- 重复副作用防护；
- Agent failure isolation；
- Parent completion aggregation；
- 全链路审计。

Agent 之间的自由文本交流只能作为内容，不能成为唯一控制协议。控制面应由稳定的
Task、Delegation、Handoff、Interaction 和 Runtime state 表达。

### Exit Gate

至少一个真实业务证明：多个 Agent 分别拥有不同信息、工具或验证责任，并且相对
Single Agent 在质量、安全或效率上产生可测增益，而不是单纯消耗更多 token。

## 10. M6：Crew

**状态：Future Hypothesis**

Multi-Agent 表示一项任务有多个执行者；Crew 表示这些执行者形成稳定、可复用的
团队配置。

```text
Complaint Resolution Crew
├── Coordinator
├── Investigator
├── Policy Specialist
├── Resolution Operator
└── Reviewer
```

Crew Definition 至少需要：

- 稳定角色与版本；
- 每个角色的能力、工具和权限；
- 委派拓扑与允许的 handoff；
- 并行与串行规则；
- 谁可以执行副作用；
- 谁承担独立审核；
- Crew budget、deadline 和 escalation；
- Crew-level Completion；
- 角色或 Agent 不可用时的替代策略。

Crew 不应被实现为固定业务 Workflow。它提供协作边界，具体工具选择与局部执行仍
由各 Hermes Agent 自主完成。

### Exit Gate

同一个版本化 Crew Definition 可以重复实例化处理多项同类 Task，角色权限、交接、
预算和验收结果可观察，并优于临时拼装的多 Agent 执行。

## 11. M7：Long-running Employee

**状态：Future Hypothesis**

Employee 不是更大的 Task，也不是 Crew 的别名。Task 有明确开始和结束；Employee
是长期存在、持续接收并负责多项 Task 的数字岗位主体。

```text
Employee
├── stable identity
├── role / job description
├── authority and permissions
├── inbox and work queue
├── active Task portfolio
├── schedule and external triggers
├── long-term work memory
├── objectives / SLA / KPI
├── manager and escalation path
└── audit and performance history
```

运行模型可能是：

```text
事件、消息、定时器或人工派单
→ Employee Inbox
→ Intake Policy
→ 创建、关联或拒绝 Task
→ 自己执行或委派给 Crew
→ 监督 waiting / overdue / escalation
→ 汇报结果
→ 继续承担下一项工作
```

这一阶段将引入新的产品问题：长期身份、跨 Task 记忆、凭据治理、工作选择、公平性、
经理监督、绩效解释、租户隔离和劳动替代风险。它不能作为当前 Task Runtime 的简单
字段扩展。

### Exit Gate

需要单独的 Product Definition 扩展、组织与身份架构、安全/权限设计、长期运行
Acceptance，以及明确的人类管理和责任边界。未完成这些工作前，不应出现
`EmployeeRegistry` 等实现。

## 12. 设计纪律：不要为未来阶段提前造地基

未来路线的作用是帮助排序，不是授权 speculative architecture。

当前代码只应增加能够直接服务 M0–M3 的抽象。以下理由不足以支持当前改造：

- “以后可能有 Multi-Agent”；
- “Crew 可能需要 DAG”；
- “Employee 可能需要通用 Memory”；
- “未来可能换成分布式队列”；
- “先把所有对象都做成多租户”；
- “先加入 Agent Registry 以后会方便”。

未来阶段应尽量复用当前已经验证的稳定原语：

```text
Task truth
Attempt / Execution
Run Request
Interaction
Contract
ExternalOperation
EvidenceSnapshot
PolicyDecision
CAS
```

但是否复用、如何组合，必须由届时的真实业务问题和新 ADR 决定。

## 13. 文档清理审计

### 13.1 立即删除建议

**当前建议：不立即删除任何受治理 Markdown 文档。**

原因不是每份文档都同样重要，而是当前存在一个 ACTIVE remediation 文档，多份
历史材料仍被 PRD、ADR、remediation 或 Authority manifest 引用。直接删除会破坏
来源追踪和文档治理检查。

### 13.2 必须保留

| 文档 | 原因 |
|---|---|
| `docs/AUTHORITY.md` | 唯一文档治理根和 Topic registry |
| `docs/PRD.md` | 当前唯一 Product Definition |
| `docs/adr/ADR-003...`、`ADR-004...` | 当前已接受的架构决策 |
| `docs/phase3/*.md` | 每份分别拥有一个当前 Runtime Design Topic，不能随意合并或删除 |
| `docs/phase4/ACCEPTANCE_MATRIX.md` | 当前 Phase 4 Engineering Acceptance authority |
| `docs/PHASE_STATUS.md` | 唯一当前实现状态汇总 |

### 13.3 可以归档，但现在不应删除

#### `docs/history/development_updated.md`

现状：已被 PRD supersede，是超长的历史产品/设计混合文档。

保留原因：PRD、Hermes 研究记录、ACTIVE remediation 和 Authority manifest 仍引用
它；其中还保存早期阶段设计演化依据。

建议：

```text
当前：保留 HISTORICAL REFERENCE
remediation 关闭后：移动到 docs/archive/
所有必要来源改为 commit/blob 引用后：可考虑从工作树删除，只保留 Git 历史
```

它是未来最主要的 Markdown 清理候选，但不是现在的删除候选。

#### `docs/history/HERMES_ARCHITECTURE_ANALYSIS.md`

现状：只适用于 Hermes 0.18.2，当前架构边界已经由 ADR-003 接管。

保留原因：它包含版本化源码研究，并被 Authority manifest 和 ACTIVE remediation
作为来源引用。

建议：remediation 关闭且 Hermes compatibility/source manifest 足以承担追溯后，
移动到 `docs/archive/hermes-0.18.2/`。除非版本研究内容已由可重建 artifact 完整替代，
否则优先归档，不建议彻底删除。

#### `严重问题整改与业务验收方案.md`

现状：ACTIVE、FROZEN 的 remediation authority。

建议：当前必须保留。全部 findings 关闭后，将 Disposition 改为 `closed`，从当前
Topic registry 解除活动地位，并按文档治理约定移动到未来的
`docs/remediation/` 周期归档位置。Remediation 是审计记录，通常应归档而非删除。

### 13.4 最强的未来删除候选

#### `docs/深入理解-AI-Agent-李博杰-v1.txt`

现状：外部背景材料，不是 Astra authority；当前只被 Hermes 历史研究记录引用。

删除前提：

1. 确认许可证和仓库存放策略；
2. 在 Hermes 研究记录中保留书名、版本、来源和必要引用；
3. 更新 `docs/AUTHORITY.md` 的 informational registry；
4. 确保没有当前设计依赖该 TXT 才能解释；
5. Documentation Governance Check 通过。

如果该文件体积大、版权不适合入库或不能稳定更新，它是最适合从工作树移除的文件；
可以保留外部来源说明，而不保存全文。

### 13.5 不建议合并的文档

以下看起来“很多”，但目前不应为了减少数量而合并：

- Phase 3 的 Task Contract、Lifecycle、Evidence、Completion、Rules、Policy、Decision；
- ADR-003 与 ADR-004；
- Walkthrough 与 Production Runtime guide；
- PRD 与 PHASE_STATUS；
- Runtime Design 与 Acceptance Matrix。

它们拥有不同权威层级或 Topic。合并会重新制造“一份文档同时定义产品、设计、
验收和状态”的问题，正是 `history/development_updated.md` 已经暴露过的问题。

## 14. 推荐的文档整理顺序

```text
现在
├── 保留所有 Authority 文档
├── 保留 ACTIVE remediation
├── 使用 Walkthrough 作为运行入口
├── 使用本 Roadmap 作为范围和先后顺序入口
└── 给历史文档保持明确 HISTORICAL 标识

M0–M3 完成、remediation 关闭后
├── 归档 development_updated.md
├── 归档 Hermes 0.18.2 analysis
├── 移动 remediation 到 docs/remediation/ 或 archive
└── 评估是否移除外部 TXT 全文

若 G1 批准 Multi-Agent 扩域
├── 修改 PRD
├── 新 ADR
├── 新 Design Topic
├── 新 Acceptance
└── 更新本 Roadmap 的状态
```

## 15. 当前推荐优先级

近期工作顺序应保持：

```text
P0  固化并演示 Single ReAct Golden Path
P1  用真实业务验证 waiting / approval / cancel / restart / retry
P2  验证第二个业务闭环，排除 complaint 硬编码
P3  建立独立 Evaluation Method 和 Demo
G1  用真实数据决定是否进入 Delegation / Multi-Agent
```

在 G1 之前，Crew 和 Employee 只用于说明长期方向，不应阻塞 MVP，也不应成为当前
Runtime 增加新抽象的理由。
