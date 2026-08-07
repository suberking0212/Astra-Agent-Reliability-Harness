# Phase 3 Task Contract

> **Documentation Governance**
> - **Role:** Normative Task Contract specification.
> - **Authority:** A3 — Runtime Design / Task Contract.
> - **Topic:** DESIGN.GOVERNANCE.TASK_CONTRACT
> - **Scope:** Versioned task objective, subjects, capabilities, constraints, authorized effects, approvals, completion references, and limits.
> - **Not Responsible For:** Execution plans, tool order, lifecycle transitions, effect normalization details, or current implementation status.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.HERMES_ASTRA_BOUNDARY
> - **Status:** FROZEN

> 状态：`FROZEN / 2026-07-20`  
> 目标：定义 Phase 3 中可版本化、可强制、可重放的任务治理根契约。Task Contract 描述允许完成什么、对哪些对象生效、允许使用哪些能力、哪些副作用被授权以及如何验收，但不描述执行步骤或工具顺序。

## 1. Contract role

Task Contract 是 Task 的不可变治理根：

```text
Task Contract
├── objective and subjects
├── immutable input snapshot
├── capability/tool boundary
├── deterministic constraints
├── authorized effect intents
├── approval requirements
├── Completion Contract reference
└── lifecycle limits
```

它同时服务于：

- Hermes prompt/context 注入；
- Tool Gateway capability、schema、scope 和 constraint enforcement；
- Effect Request normalization；
- Approval Requirement matching；
- Evidence Collection 的 subject/scope 选择；
- Requirement Evaluation 与 replay；
- Policy/Runtime 的边界和限额判定。

`RuntimeInvocation.task_contract: Mapping[str, Any]` 可以继续作为 Adapter 传输兼容层，但持久化和进入 Runtime 前必须验证为本契约。任意 mapping 或自然语言不能成为 Phase 3 的规范 Task Contract。

## 2. Normative envelope

第一版字段：

```text
schema_version
contract_id
contract_version
contract_hash
task_type
execution_type
objective
subject_refs[]
input_snapshot
allowed_capabilities[]
resolved_tools[]
constraints[]
authorized_effects[]
approval_requirements[]
completion_contract_ref
limits
```

除明确标记为描述性字段外，所有字段都进入 `contract_hash`。Task 创建后不得原地修改 Contract；变更任务意图必须创建新 `contract_version`，并由 Runtime 明确决定是否仍属于同一 Task。第一版不允许运行中的 Task 自动升级 Contract。

示例：

```json
{
  "schema_version": "1",
  "contract_id": "contract-refund-order-o123",
  "contract_version": "1",
  "contract_hash": "sha256:...",
  "task_type": "refund_resolution",
  "execution_type": "tool_execution",
  "objective": {
    "description": "Resolve the approved refund request for order O123."
  },
  "subject_refs": [
    {"authority_domain": "commerce.example", "type": "order", "id": "O123"}
  ],
  "input_snapshot": {
    "schema_id": "refund.request",
    "schema_version": "1",
    "values": {"amount_minor": 50000, "currency": "CNY"},
    "content_hash": "sha256:..."
  },
  "allowed_capabilities": [
    {"capability_id": "orders.read", "capability_version": "1"},
    {"capability_id": "refunds.issue", "capability_version": "1"}
  ],
  "resolved_tools": [
    {
      "tool_name": "issue_refund",
      "tool_version": "1",
      "schema_hash": "sha256:...",
      "capability_ref": "refunds.issue@1",
      "access_mode": "effect"
    }
  ],
  "constraints": [
    {
      "constraint_id": "refund.amount_scope",
      "constraint_version": "1",
      "kind": "parameter_scope",
      "enforcement_point": "tool_gateway",
      "configuration": {"amount_minor": 50000, "currency": "CNY"}
    }
  ],
  "authorized_effects": [
    {
      "effect_intent_id": "refund-order-o123-once",
      "effect_type": "finance.refund",
      "effect_type_version": "1",
      "authority_domain": "payments.example/merchant-main",
      "subject_ref": {"authority_domain": "commerce.example", "type": "order", "id": "O123"},
      "parameter_constraints": {"amount_minor": 50000, "currency": "CNY", "destination": "original_payment_method"},
      "max_confirmed_occurrences": 1,
      "approval_requirement_ref": "refund-over-threshold@1"
    }
  ],
  "approval_requirements": [
    {
      "approval_requirement_id": "refund-over-threshold",
      "approval_requirement_version": "1",
      "effect_intent_refs": ["refund-order-o123-once"],
      "risk_class": "high",
      "approver_policy_ref": "finance.manager-over-amount@1",
      "usage_semantics": "single_effect_single_use"
    }
  ],
  "completion_contract_ref": {"contract_id": "completion-refund", "contract_version": "1"},
  "limits": {
    "max_attempts": 3,
    "task_deadline": "2026-07-21T00:00:00Z",
    "max_executions_per_attempt": 4,
    "max_feedback_cycles": 2,
    "max_reconcile_cycles": 3
  }
}
```

## 3. Objective and subject scope

`objective.description` 是给 Hermes 和人类审计者的稳定目标摘要，但不能替代结构化 scope，也不能单独授权副作用。

每个 `subject_ref` 必须包含：

```text
authority_domain
type
id
version, optional
```

`authority_domain` 防止两个系统中同名对象被错误视为同一对象。需要字段级写入限制时，必须使用 `constraints` 或 `authorized_effects.parameter_constraints`，不能只写在 objective 中。

## 4. Immutable input snapshot

`input_snapshot` 固定任务创建时已知且会影响治理判断的输入：

```text
schema_id
schema_version
values
content_hash
```

敏感值可以存为受控引用、密文或不可逆摘要，但 Snapshot 的逻辑内容必须可版本化。后续用户输入不修改旧 Snapshot；Interaction resolution 作为新的权威记录进入后续 DecisionContext。若输入改变了任务目标、subject、写入 scope 或 effect intent，必须创建新的 Contract version，而不是普通 Interaction resolution。

## 5. Capability and resolved tool boundary

`allowed_capabilities` 表示稳定业务能力；`resolved_tools` 是针对当前环境解析出的执行表面。

每个 resolved tool 至少固定：

```text
tool_name
tool_version
schema_hash
capability_ref
access_mode = read | effect | runtime_primitive | result_submission
```

Gateway 必须同时校验 capability、tool identity 和 schema identity。Tool/plugin 升级导致 schema hash 变化时，旧 Task 不得静默采用新语义；必须经过兼容性解析或创建新 Contract version。

Tool 是执行入口，不是副作用身份。相同副作用可能经不同 Tool/Adapter 执行；同一个 Tool 也可能产生不同 effect identity。

## 6. Deterministic constraints

每个 constraint 固定：

```text
constraint_id
constraint_version
kind
enforcement_point
configuration
```

`configuration` 由已注册的 constraint handler 拥有，Core 不建立通用规则 DSL。未知 constraint、未知版本或无法加载 handler 时，受影响动作必须 fail closed。

第一版允许的 enforcement point：

```text
runtime
tool_gateway
effect_normalizer
completion_gate
```

Constraint 必须回答确定性的“是否允许”，不得指定 Hermes 的下一 Tool、参数修复步骤或业务 Workflow。

## 7. Authorized effect intents

所有有外部副作用的动作都必须匹配一个 `authorized_effects[]` 条目。该条目是 Task 授权边界，不是已发生的 ExternalOperation。

字段：

```text
effect_intent_id
effect_type / effect_type_version
authority_domain
subject_ref
parameter_constraints
max_confirmed_occurrences
approval_requirement_ref, optional
```

`effect_intent_id` 在同一 Contract version 内唯一，用于区分业务上有意发生的两个参数相同副作用。例如“向同一收件人发送两封内容相同但用途不同的邮件”必须声明两个 effect intent；不得通过随机 idempotency key 绕过唯一性。

副作用调用前，Effect Normalizer 必须证明请求匹配且只匹配一个 effect intent。零匹配或多重匹配都必须拒绝。

## 8. Approval requirements

Task Contract 只声明哪些 effect intent 需要哪一种批准。具体 Approval Record/Token 必须绑定运行时规范化后的 `effect_identity`，详见 `APPROVAL_BINDING.md`。

每个 requirement 至少固定：

```text
approval_requirement_id / approval_requirement_version
effect_intent_refs[]
risk_class
approver_policy_ref
usage_semantics
validity_policy, optional
```

Approval requirement 不得只引用 Tool 名称或宽泛自然语言。

## 9. Completion and limits

`completion_contract_ref` 必须固定 Completion Contract ID/version。Task Contract 不内联或重新解释 Requirement；Hermes 也不能提交或修改 Completion Contract。

`limits` 固定 Task/Attempt 治理上限，至少支持：

```text
max_attempts
task_deadline
max_executions_per_attempt
attempt_deadline, optional
max_feedback_cycles
max_reconcile_cycles
```

Limits 的 Runtime 计数属于权威状态，不写回 Task Contract。

## 10. Canonicalization and hash

`contract_hash` 使用：

```text
sha256(JCS(canonical contract without contract_hash and descriptive-only metadata))
```

第一版使用 JSON Canonicalization Scheme 等价语义：UTF-8、对象键排序、无无意义空白、数组保持顺序、禁止 NaN/Infinity。若实现不直接采用 RFC 8785，必须固定等价 serializer version 并纳入 `schema_version`。

引用 Contract 的 Receipt、DecisionContext、EffectRequest、Approval 和 EvidenceSnapshot 必须保存 `contract_id`、`contract_version` 和 `contract_hash`。

## 11. Mutation and compatibility rules

- Task 创建后 Contract append-only/versioned，不原地更新；
- 描述性文本修改若不进入 hash，必须明确标记，不得影响强制语义；
- subject、input、capability、tool schema、constraint、effect intent、approval requirement、completion ref 或 limits 变化都产生新 Contract version；
- 运行中 Contract 版本变化使旧 DecisionContext 和未执行的 Approval token stale；
- 已发生 ExternalOperation 仍引用发生时的 Contract version，不随 Task 升级重写；
- 新版本不得重新授权已经超过 `max_confirmed_occurrences` 的旧 effect intent。

## 12. Explicit non-goals

Task Contract 不包含：

- ordered steps；
- Workflow/DAG；
- next tool；
- business plan；
- Hermes reasoning trace；
- 动态 Policy 语言；
- 任意 Completion 布尔 DSL；
- 当前 Task/Attempt/Execution 可变状态。

## 13. Contract tests

- 任意 mapping 未通过 schema 校验不能进入 Phase 3 Runtime；
- 相同语义输入产生相同 `contract_hash`；
- subject/scope/constraint/tool schema/effect intent 变化改变 hash；
- effect request 必须唯一匹配一个 authorized effect；
- read tool 不需要 effect intent，effect tool 必须有 effect intent；
- 未知 constraint/version fail closed；
- Contract version 变化使旧 Decision/Approval stale；
- Contract 不允许 ordered steps、next tool 或 workflow 字段；
- 业务插件新增字段不能进入 Core envelope，只能进入已版本化的 opaque configuration；
- Hermes 私有类型不进入 Contract。
