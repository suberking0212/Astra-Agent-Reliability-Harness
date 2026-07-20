# Phase 3 Contract Scenario Validation — Round 1

> 日期：2026-07-20  
> 状态：`HISTORICAL ROUND 1 / BLOCKERS RESOLVED IN ROUND 2`  
> 范围：只验证 Contract 的表达力和端到端职责闭合，不实现 Phase 3 Runtime。

> 后续结论：三个 Core 阻塞项已由 `TASK_CONTRACT.md`、`EFFECT_IDENTITY_AND_IDEMPOTENCY.md`、`APPROVAL_BINDING.md` 和 Round 2 fixtures 关闭。正式冻结证据见 `CONTRACT_SCENARIO_VALIDATION_ROUND2.md`。

## 1. 验证目标与判定口径

本轮用 7 个典型业务压力测试以下链路：

```text
Task Contract
→ Hermes execution
→ Tool / Constraint Boundary
→ Receipt / ExternalOperation / Evidence Collection
→ Requirement Evaluators / Completion Aggregator
→ Task Policy
→ Runtime completion gate
```

每个场景回答：

1. Task Contract 能否表达；
2. Completion Contract 能否表达；
3. Hermes 是否知道怎么执行；
4. 是否需要新的 Tool；
5. Evidence 能否收集；
6. Requirement 是否能判断；
7. Policy 是否能决定下一步；
8. Runtime 是否能够最终完成。

判定分为：

- `YES`：现有 Core Contract 已足够，业务差异留在插件、Adapter、Collector 或 Evaluator；
- `CONDITIONAL`：架构可以覆盖，但必须补充业务 Tool、权威查询能力或明确业务语义；
- `CORE GAP`：在冻结前必须修改或补齐 Core Contract；
- `NOT IMPLEMENTED`：设计可表达，但 Phase 3 Runtime 当前尚未实现。

“需要新 Tool”不等于“需要新 Core Tool”。遵循 Hermes narrow-waist 原则，Jira、CRM、采购、邮件等能力应优先通过 service-gated tool、Plugin 或 MCP 提供。

## 2. 总结矩阵

| 场景 | Task | Completion | Hermes | Tool | Evidence | Requirement | Policy | Runtime design |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 客服投诉 | CORE GAP | YES | YES | 业务 Tool | YES | YES | YES | YES |
| 退款审批 | CORE GAP | YES | YES | 退款/审批 Adapter | CONDITIONAL | YES | YES | YES |
| 创建 Jira 工单 | CORE GAP | YES | YES | Jira Plugin/MCP | YES | YES | YES | YES |
| CRM 更新客户信息 | CORE GAP | YES | YES | CRM Plugin/MCP | YES | YES | YES | YES |
| 采购审批 | CORE GAP | YES | YES | 采购 Adapter | CONDITIONAL | YES | YES | YES |
| 邮件发送 | CORE GAP | YES | YES | Mail Plugin/MCP | CONDITIONAL | YES | YES | YES |
| Browser Agent 自动填表 | CORE GAP | YES | YES | 现有 Browser + 受控提交边界 | CONDITIONAL | YES | YES | YES |

矩阵中的 `Task = CORE GAP` 不是说这些业务无法建模，而是当前规范只有 Task Contract 的最小字段列表和 `Mapping[str, Any]` 传输形态，没有可版本化、可校验、可重放的规范实体。所有场景都需要把目标对象、允许修改范围、确定性约束和审批绑定放入稳定字段；继续依赖自然语言或任意 mapping 会使后续 Evidence、Requirement、Policy 和重放语义不稳定。

## 3. 场景验证

### 3.1 客服投诉

示例目标：调查损坏订单，创建投诉工单；若满足条件并获批则退款，否则记录可验证的替代处理结果。

- **Task Contract：CORE GAP。** `task_type`、`allowed_tools`、`completion_requirements` 可以描述能力边界，但缺少规范化的订单/客户 subject、允许的解决范围、退款上限和确定性 constraints 字段。
- **Completion Contract：YES。** 可拆为“投诉工单存在且关联正确对象”“所选处理结果已持久化”“required ExternalOperation 已 confirmed”“必要通知已记录”等 Requirement；条件分支可先由一个版本化业务 Evaluator 判断“最终处理结果符合政策”，不要求 Core 引入布尔 DSL。
- **Hermes：YES。** Phase 2 已证明 Hermes 能自主查询订单、检索政策、创建工单并提交结果，不需要固定 Workflow。
- **Tool：业务扩展。** Phase 2 已有投诉查询/创建工具；退款、换货、通知仍需业务 Tool，但不应进入 Hermes Core。
- **Evidence：YES。** Receipt、ComplaintTicket 读回、ExternalOperation 和业务状态 Observation 足够。
- **Requirement：YES。** Complaint/Refund Evaluator 插件可纯函数评估固定 Snapshot。
- **Policy：YES。** 缺信息时 `request_input`，缺审批时 `request_approval`，远端状态不明时 `reconcile`，可修复时 `continue_with_feedback`。
- **Runtime：设计 YES，当前 NOT IMPLEMENTED。** completion gate 能闭合；Phase 3 生命周期与 CAS 尚未实现。

### 3.2 退款审批

示例目标：验证订单资格；超过自动退款阈值时请求主管批准；批准后只退款一次并确认账务状态。

- **Task Contract：CORE GAP。** 需要规范字段绑定退款对象、币种/金额、风险规则、审批主体以及“批准只适用于哪一个规范化请求”。当前只有非规范化 `approval_requirements` 描述。
- **Completion Contract：YES。** Requirement 可分别验证退款资格、approval resolution、退款 ExternalOperation confirmed、账务状态和金额一致。
- **Hermes：YES。** Hermes 能解释政策和结构化 `approval_required`，并在恢复 turn 后继续；它不拥有审批权威。
- **Tool：需要退款 Tool 和只读账务查询。** 写 Tool 必须支持 idempotency key；查询能力用于 confirmation/reconciliation。
- **Evidence：CONDITIONAL。** 支付系统必须提供 idempotency key、operation ID 或退款查询。若写成功后无法查询，只能得到 `indeterminate`，Contract 正确拒绝伪造 exactly-once。
- **Requirement：YES。** Refund Evaluator 可检查金额、订单、批准范围和远端状态。
- **Policy：YES。** 映射到 `request_approval`、`reconcile`、`continue_with_feedback`、`fail/escalate`。
- **Runtime：设计 YES，当前 NOT IMPLEMENTED。** 关键是审批 token 与规范化 effect identity/request hash 的精确绑定。

### 3.3 创建 Jira 工单

示例目标：为指定项目创建一个包含标题、描述、优先级和关联客户的 Jira Issue，并返回可访问的 issue key。

- **Task Contract：CORE GAP。** 需要不可变 project、issue type、字段允许列表、去重键和内容边界。只把这些写在 `user_request` 中不足以支撑权限校验和重放。
- **Completion Contract：YES。** 验证 issue 存在、project/type/关键字段匹配、唯一性满足、Receipt/remote state 可追溯。
- **Hermes：YES。** 结构化 Tool schema 和清晰描述足以让 Hermes 选择创建或查询动作。
- **Tool：需要 Jira Plugin/MCP 或 service-gated tool。** 不需要修改 Hermes Core，也不需要修改 Astra Core Contract。
- **Evidence：YES。** create response、ExecutionReceipt、issue GET 及 Jira version/update timestamp 可形成 Snapshot。
- **Requirement：YES。** Jira Evaluator 只读取 Snapshot，Aggregator 不理解 Jira 字段。
- **Policy：YES。** 字段缺失请求输入；权限失败可修复则反馈；状态未知则 reconcile；不可恢复则 fail/escalate。
- **Runtime：设计 YES，当前 NOT IMPLEMENTED。** 使用 client token/idempotency marker 或业务去重查询避免重复 issue。

### 3.4 CRM 更新客户信息

示例目标：把指定客户的电话和地址更新为用户确认值，不修改信用等级、owner 或营销 consent。

- **Task Contract：CORE GAP。** 这是最明显的字段级 scope 场景：必须持久化 target customer、允许修改字段、期望值、禁止字段和可选 expected source version。当前 Contract 没有规范化 task inputs/constraints。
- **Completion Contract：YES。** 验证目标字段等于期望值、禁止字段未改变、业务版本已推进且写操作可追溯。
- **Hermes：YES。** Hermes 可先查询、消歧、必要时请求输入，再调用 patch Tool。
- **Tool：需要 CRM get/patch Tool。** Patch 应接受字段白名单和 optimistic concurrency token，而不是暴露任意对象写入。
- **Evidence：YES。** 更新前/后带版本 Snapshot、Receipt 和 CRM audit record 足够。
- **Requirement：YES。** CRM Evaluator 可同时验证 positive mutation 和 forbidden-field non-mutation。
- **Policy：YES。** 客户身份不唯一时 `request_input`；版本冲突可 `continue_with_feedback` 或新 Attempt；权限不足时 fail/escalate。
- **Runtime：设计 YES，当前 NOT IMPLEMENTED。** CAS 保护 Astra 状态；CRM 自身版本/ETag 保护远端 lost update。

### 3.5 采购审批

示例目标：提交采购申请；预算超过阈值或供应商不在白名单时等待指定角色批准；最终确认采购单状态。

- **Task Contract：CORE GAP。** 需要采购对象、金额/币种、成本中心、供应商、审批规则版本、批准角色和审批 subject binding。
- **Completion Contract：YES。** 可验证 requisition/PO 存在、金额和供应商匹配、必要审批已解决、最终状态符合业务规则。
- **Hermes：YES。** Hermes 负责准备申请、解释缺失信息和选择 Tool；Astra/业务系统决定是否允许提交或批准。
- **Tool：需要采购系统 Adapter。** 创建申请、查询状态、可选提交审批；人的治理批准统一走 Runtime Interaction，不另建第二套等待状态机。
- **Evidence：CONDITIONAL。** 若采购系统有工作流审计和状态查询则完整；否则批准发生在外部系统但无法固定读取时只能 `unknown/indeterminate`。
- **Requirement：YES。** Procurement Evaluator 负责业务审批链语义，Core Aggregator 保持 `all_required`。
- **Policy：YES。** `request_input`、`request_approval`、`reconcile`、`fail/escalate` 足够；无需新增 `wait_for_workflow` 动作。
- **Runtime：设计 YES，当前 NOT IMPLEMENTED。** 长时间等待由持久化 Interaction 或 reconciliation job 表达，不保留 Python 栈。

### 3.6 邮件发送

示例目标：向固定收件人发送固定用途的邮件，禁止额外收件人和附件，返回 provider message ID。

- **Task Contract：CORE GAP。** 收件人、允许的抄送范围、内容目的、附件约束、敏感信息约束和是否需要审批必须是可强制执行的结构化 scope。
- **Completion Contract：YES。** 但必须先定义完成语义是 `accepted_by_provider`、`sent` 还是 `delivered`。Evaluator 只能验证 Contract 声明的层级，不能把 SMTP/API accepted 当作 delivered。
- **Hermes：YES。** Hermes 能草拟内容并调用 send Tool；最终 recipient/body/attachment 约束必须由 Gateway 校验而不是只靠 prompt。
- **Tool：需要 Mail Plugin/MCP。** 高风险发送应支持预览/审批和稳定 idempotency marker；不应新增通用 Core Tool。
- **Evidence：CONDITIONAL。** Provider message ID 和 sent-items 查询可证明 accepted/sent；delivery 需要 delivery event/DSN。没有 delivery authority 时应返回 `unknown`。
- **Requirement：YES。** Mail Evaluator 按明确的 delivery semantics、recipient scope 和 content hash 判断。
- **Policy：YES。** 缺内容请求输入，缺批准请求审批，发送结果不明进行 reconcile，永久拒收则 fail/escalate。
- **Runtime：设计 YES，当前 NOT IMPLEMENTED。** 响应丢失时必须按 message id/client token 查询，不能盲目重发。

### 3.7 Browser Agent 自动填表

示例目标：登录外部网站，填写指定表单；提交前需人工批准；提交后确认服务端申请编号和状态。

- **Task Contract：CORE GAP。** 需要目标 origin、允许页面/动作、字段 scope、敏感字段规则、是否允许最终 submit、审批绑定和目标业务对象。仅靠 `allowed_tools = browser_*` 过宽。
- **Completion Contract：YES。** 可验证服务端申请记录、确认编号、字段摘要、提交时间和 required approval；截图/DOM 只能作为辅助证据，不能单独证明服务端提交成功。
- **Hermes：YES。** Hermes 已具备 Browser Agent 执行能力，能动态导航和填表；页面变化属于局部执行适应。
- **Tool：通常不需要新的 Hermes Core Tool。** 需要把现有 Browser 工具置于 Astra 受控副作用边界；若网站有 API/查询接口，应增加站点 Adapter/Collector 作为更强权威来源。
- **Evidence：CONDITIONAL。** 最佳证据是服务端 confirmation ID/API 查询；其次是稳定的账户历史页。只有截图、toast 或 DOM 文本时 authority 较弱，写入成功但响应丢失会进入 `indeterminate`。
- **Requirement：YES。** Site-specific Evaluator 可验证固定 Snapshot；Core 不理解 DOM selector。
- **Policy：YES。** 缺字段请求输入，提交前请求审批，提交状态不明 reconcile，页面失败由 Hermes 局部处理。
- **Runtime：设计 YES，当前 NOT IMPLEMENTED。** 必须把“导航/填写”与“最终提交”区分风险，并对提交建立 ExternalOperation；否则无法安全恢复。

## 4. 跨场景结论

### 4.1 已经证明可保持稳定的 Core

以下设计无需因本轮业务场景增加业务专用逻辑：

- Hermes/Astra 职责边界；
- Task / Attempt / Execution / Interaction 生命周期分离；
- CompletionRequirement + versioned Evaluator Registry；
- `satisfied / unsatisfied / unknown / evaluator_error` 和 `all_required` Aggregator；
- Receipt、ExternalOperation、Evidence Collection、immutable EvidenceSnapshot；
- `complete / continue_with_feedback / start_new_attempt / request_input / request_approval / reconcile / fail / escalate`；
- DecisionContext、幂等 decision key、CAS 和 derived-object exactly-once；
- Runtime completion gate；
- “业务能力在 Plugin/MCP/Adapter，Core 保持 narrow waist”的 Tool 扩展方式。

没有场景要求 Core 增加固定 Workflow、DAG、Planner、业务 Tool 顺序、新 Policy action 或业务字段 switch。

### 4.2 冻结阻塞项：规范化 Task Contract

冻结前必须新增一份独立的规范性 `TASK_CONTRACT.md`，至少固定：

```text
schema_version
contract_id / contract_version
task_type / execution_type
objective
subject_refs
input_snapshot or immutable task parameters
allowed_capabilities
resolved allowed_tools + tool/schema identity
deterministic constraints
approval requirements
completion_contract_id / version
task/attempt limits references
```

约束必须可由 Gateway/Runtime 强制，而不是只作为自然语言 prompt。`RuntimeInvocation.task_contract: Mapping[str, Any]` 可以继续作为传输兼容层，但不能作为 Phase 3 的规范领域模型。

### 4.3 冻结阻塞项：Approval binding

`approval_requirements` 和 `interaction_spec.approval_subject` 需要固定结构，至少能绑定：

```text
approval_requirement_id / version
operation_type
subject_ref
normalized request hash or effect identity
risk class
approver scope/role
expiry and single-use/reuse semantics
```

否则“批准退款”可能被错误复用于不同金额、不同订单或不同收件人的副作用调用。

### 4.4 冻结阻塞项：Canonical effect identity

ExternalOperation、idempotency、duplicate-side-effect Rule、审批凭证和恢复对账都依赖同一个规范化副作用身份，但当前文档只分别提到 `operation_type`、`request_hash`、`idempotency_key` 和“normalized effect identity”，没有定义它们之间的稳定关系。

冻结前应固定：

```text
effect_type
subject_ref
normalized_parameters_hash
effect_identity
idempotency_scope
approval_binding_ref
```

Tool/业务 Adapter 负责规范化业务参数，Core 只规定 envelope、hash/version 和唯一性语义，不理解退款、邮件或 Jira 字段。

## 5. 非阻塞但必须写入契约测试的结论

- Tool 可用不代表 Evidence 可得；每个有副作用 Tool 必须声明 confirmation/reconciliation 能力。
- 无查询、无幂等的外部写操作必须可稳定落为 `indeterminate`。
- “邮件已发送”“表单已提交”等完成语义必须由 Requirement 明确 authority level，不能由自然语言模糊决定。
- Screenshot、DOM、Hermes Tool Result 和 Agent 自述不能单独成为强业务完成证据。
- 字段级禁止修改必须能由 Task constraints 强制，并由 before/after Evidence 验证。
- 业务 Tool 增加只扩展 Plugin/MCP/Adapter/Evaluator Registry，不修改 Aggregator、Policy action set 或 Runtime 状态集合。
- 完成已满足但 Hermes 最终说明缺失时，Runtime 仍应按既有 completion gate 完成并提供降级摘要。

## 6. Round 1 冻结建议

本轮结论：

```text
Scenario coverage: PASS WITH CORE GAPS
Completion/Evidence/Policy architecture: PASS
Hermes execution boundary: PASS
Tool extensibility model: PASS
Task Contract normative schema: FAIL
Approval/effect binding: FAIL
Phase 3 design freeze: NO
```

完成以下工作后应进行 Round 2：

1. 冻结规范化 Task Contract；
2. 冻结 Approval binding；
3. 冻结 canonical effect identity；
4. 将本报告 7 个场景转成 table-driven contract fixtures；
5. 对每个场景至少覆盖 success、missing input、approval required、response lost/indeterminate、stale decision 五类路径；
6. 验证这些 fixtures 不要求修改 Core Aggregator、Policy action set 或生命周期状态机。

若 Round 2 全部通过且不再修改 Core Contract，才建议正式冻结 Phase 3 设计。
