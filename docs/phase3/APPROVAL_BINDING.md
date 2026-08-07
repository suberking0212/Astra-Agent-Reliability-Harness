# Phase 3 Approval Binding Contract

> **Documentation Governance**
> - **Role:** Normative approval-binding specification.
> - **Authority:** A3 — Runtime Design / Approval Binding.
> - **Topic:** DESIGN.GOVERNANCE.APPROVAL
> - **Scope:** Approval requirements, requests, resolutions, credentials, exact matching, consumption, and approval interaction semantics.
> - **Not Responsible For:** Defining effect identity, the general Task lifecycle, business policy, or operator UI/API.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.HERMES_ASTRA_BOUNDARY, DESIGN.GOVERNANCE.TASK_CONTRACT, DESIGN.GOVERNANCE.EFFECT_IDENTITY, DESIGN.GOVERNANCE.LIFECYCLE
> - **Status:** FROZEN

> 状态：`FROZEN / 2026-07-20`  
> 目标：保证一次批准只授权 Task Contract 中声明且经过规范化的精确副作用，不被复用于其他对象、参数、权限范围或 Contract version。

## 1. Core invariant

Approval 的授权对象固定为：

```text
Task Contract approval requirement
+ exact CanonicalEffectRequest
+ exact effect_identity
+ approver policy result
+ explicit validity and usage semantics
```

Approval 不得只绑定：

```text
Task
Tool name
operation type
subject only
natural-language prompt
"允许退款" / "允许发送" 等宽泛文本
```

正式不变量：

```text
approved effect_identity == executed ExternalOperation.effect_identity
```

任一受治理字段变化都会生成新的 effect identity，并使旧 Approval 不适用。

## 2. ApprovalRequirement

ApprovalRequirement 由 Task Contract 声明：

```text
approval_requirement_id
approval_requirement_version
effect_intent_refs[]
risk_class
approver_policy_ref
usage_semantics
validity_policy, optional
```

`effect_intent_refs` 必须引用同一 Task Contract 中的 authorized effects。Requirement 可以适用于一个或多个 effect intent，但一次具体 Approval Resolution 必须绑定其中一个规范化后的 exact effect identity。

`approver_policy_ref` 引用版本化的身份/角色/额度策略。Core 不实现企业组织规则 DSL；策略 Adapter 只返回可审计的授权判定和 policy version。

第一版 `usage_semantics`：

```text
single_effect_single_use
single_effect_reusable_until_expiry
```

默认和推荐值为 `single_effect_single_use`。即使允许在 expiry 前复用，也只能复用于同一个 effect identity 的安全 retry/reconciliation，不能授权第二个 ExternalOperation 或不同 effect identity。

## 3. ApprovalRequest

当 Gateway 或 Hermes 发现精确 effect 需要审批时，Runtime 持久化：

```text
approval_request_id
interaction_id
task / attempt / execution refs
task_contract_ref
approval_requirement_ref
effect_identity
effect_request_hash
effect_summary
permission_scope
risk_class
approver_policy_ref
requested_at
expires_at, optional
status
version
```

`effect_summary` 用于 UI 和人类判断，至少应显示 subject、规范化关键参数和 authority domain，但它不是机器匹配权威。机器匹配只使用固定引用、hash、identity 和版本。

`permission_scope` 是 Approval 允许的治理动作范围，例如：

```text
execute_effect
```

第一版不允许 Approval 携带 `next_tool`、ordered steps 或 business plan。

## 4. ApprovalResolution

审批解决后持久化不可变 Resolution：

```text
approval_resolution_id
approval_request_id
interaction_id
decision = approved | denied
effect_identity
effect_request_hash
approval_requirement_ref
approver_subject_ref
approver_policy_ref / evaluated policy version
permission_scope
resolved_at
valid_from
expires_at, optional
usage_semantics
resolution_version
evidence_refs
reason, optional
```

Resolution 必须保存审批时显示和验证的 exact request hash。审批界面或外部审批系统返回的自然语言说明只能作为 evidence/reason，不能扩大 permission scope。

拒绝结果也是权威记录。Hermes 可以收到结构化反馈并选择非受限替代方案，但不得通过新 Tool 名、参数别名或新 Attempt 重试相同被拒绝的 effect identity，除非新的 Task Contract/Approval Requirement 明确允许重新申请。

## 5. ApprovalCredential

Tool Gateway 执行 effect 前使用 Runtime 签发的短期 credential 或权威 Resolution 引用：

```text
credential_id
approval_resolution_id
task_contract_ref
approval_requirement_ref
effect_identity
effect_request_hash
permission_scope
issued_at / expires_at
usage_semantics
credential_version
signature_or_store_reference
```

Credential 只是对权威 ApprovalResolution 的可验证引用，不是新的授权来源。可以是签名 token，也可以是只含 opaque ID、由 Gateway 回查 Store 的引用。

若使用签名 token，必须防篡改并绑定全部上述字段；若使用 Store lookup，必须在消费时读取 Resolution 当前权威状态和版本。

## 6. Gateway match algorithm

副作用调用前顺序固定：

```text
1. validate Task Contract and resolved tool/schema
2. normalize raw request
3. uniquely match authorized effect intent
4. compute effect_request_hash and effect_identity
5. find applicable ApprovalRequirement
6. if required, load and verify ApprovalResolution/Credential
7. atomically reserve/consume approval usage and prepare ExternalOperation
8. call external authority
```

Approval match 必须全部满足：

```text
approved decision
same task_contract id/version/hash
same approval requirement id/version
same effect identity
same effect request hash
same permission scope
approver policy satisfied at recorded version
within validity window
usage not exhausted/revoked
Task/Attempt not cancelled or terminal
```

任一不满足返回结构化 deny，例如：

```text
approval_required
approval_denied
approval_expired
approval_scope_mismatch
approval_effect_mismatch
approval_already_consumed
approval_stale_contract
```

这些稳定错误类型属于 Gateway/Runtime boundary；Hermes 可以解释并请求新的批准，但 Core Policy 不依赖 Hermes 私有错误字符串。

## 7. Atomic consumption and ExternalOperation

对 `single_effect_single_use`，以下本地变化必须在一个事务内完成：

```text
verify approval remains usable
mark approval usage reserved/consumed for effect identity
create or reuse prepared ExternalOperation for same effect identity
write Fact/Outbox
```

事务崩溃只能完整提交或回滚。若 ExternalOperation 已存在，则 replay 应关联同一 operation，不应把 Approval 计为第二次消费。

Approval consumed 不等于外部 effect confirmed。外部调用失败或 response loss 后，Runtime 继续 reconcile 同一 ExternalOperation；不要求重新批准同一 identity 的安全恢复，除非 validity policy 明确要求或 Contract 已变化。

## 8. Interaction lifecycle

Hermes 主动 `request_approval` 和 Gateway 返回 `approval_required` 必须汇合到同一 Runtime 路径：

```text
CanonicalEffectRequest
→ ApprovalRequest
→ Interaction(kind=approval)
→ Task waiting_approval
→ ApprovalResolution
→ Interaction resolved
→ new Execution / same Attempt by default
```

Hermes 主动请求时，如果尚未提供可规范化的 exact effect request，Runtime 可以先创建 `request_input` 或返回结构化反馈要求 Hermes 形成具体候选操作；不得为一个未确定金额、对象或收件人的宽泛动作签发 Approval。

Interaction resolution 不恢复 Python 调用栈。新 Execution 读取 ApprovalResolution、Contract、EffectRequest 和同一 Hermes Session/结构化反馈继续执行。

## 9. Contract and request changes

以下变化使旧 Approval stale：

- Task Contract id/version/hash；
- effect intent；
- authority domain；
- effect type/version；
- subject；
- normalized parameters；
- normalizer version；
- approval requirement/version；
- permission scope；
- approver policy version（若 policy 要求重新授权）；
- expiry/revocation/usage exhaustion。

仅 Tool call ID、Execution ID、Attempt ID 或安全 retry 次数变化不改变 effect identity，也不要求新的批准。

如果用户在 approval UI 中修改金额、收件人或其他业务参数，该动作不是“批准并修改”，而是拒绝/取消旧请求并产生新的 CanonicalEffectRequest 和 ApprovalRequest。

## 10. Evidence and completion

EvidenceSnapshot 对需要审批的 effect 至少固定：

```text
ApprovalRequirement ref
ApprovalRequest/Resolution refs + versions
effect_identity / effect_request_hash
approval decision / scope / validity / usage
ExternalOperation ref + status
authoritative business observation refs
```

Requirement Evaluator 可以验证业务是否要求并获得了正确批准；Runtime completion gate 仍负责阻止 required ExternalOperation 处于 prepared/in_flight/acknowledged/indeterminate。

Approval satisfied 不等于 Completion satisfied，Approval approved 也不证明外部业务状态已改变。

## 11. Policy boundary

Task Policy 可以输出 `request_approval`，但只能携带：

```text
approval_requirement_ref
effect_identity
effect_request_hash
permission_scope
evidence refs
active constraints
```

Policy 不生成 Approval、Credential 或 Tool plan。Runtime 负责创建 Interaction；Gateway 负责 exact match；Approver policy Adapter 负责身份/角色判定。

当 Approval denied/expired/stale：

- 存在合法替代结果时，可 `continue_with_feedback`；
- 新的精确 effect 需要批准时，可再次 `request_approval`；
- 无合法完成路径时 `fail` 或 `escalate`。

无需增加新的 Policy Action。

## 12. Explicit non-goals

- 不实现组织架构或额度审批 DSL；
- 不允许批准整个 Tool、Task 或一类未来未知操作；
- 不允许 Approval 指定 Hermes 执行计划；
- 不把批准视为外部副作用成功证据；
- 不通过自然语言 prompt 做机器授权匹配；
- 不建立第二套 approval waiting 状态机。

## 13. Contract tests

- 订单、金额、币种、destination、recipient、subject 或 Contract version 变化使旧 Approval 不匹配；
- Tool/Execution/Attempt retry 对同一 effect identity 可复用同一 Approval 进行安全恢复；
- Approval 只绑定 exact effect identity，不只绑定 Tool/effect type；
- `single_effect_single_use` 与 ExternalOperation prepare 原子消费；
- replay 同一 ExternalOperation 不产生第二次消费；
- denied/expired/revoked/stale Approval fail closed；
- 无 exact CanonicalEffectRequest 时不能签发 Approval；
- Approval evidence 不会被 Aggregator 当作业务 effect confirmed；
- Policy/feedback 不包含 next tool、ordered steps 或 workflow；
- 主动审批和强制审批创建同一类 Interaction/Resolution。
