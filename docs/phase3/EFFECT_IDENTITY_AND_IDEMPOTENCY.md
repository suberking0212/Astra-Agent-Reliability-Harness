# Phase 3 Canonical Effect Identity and Idempotency Contract

> **Documentation Governance**
> - **Role:** Normative effect identity and idempotency specification.
> - **Authority:** A3 — Runtime Design / Effect Identity.
> - **Topic:** DESIGN.GOVERNANCE.EFFECT_IDENTITY
> - **Scope:** CanonicalEffectRequest, normalization, hashes, effect identity, ExternalOperation identity, idempotency, and reconciliation identity.
> - **Not Responsible For:** Approval lifecycle, Task lifecycle, business planning, scheduling, or acceptance results.
> - **Depends On:** GOVERNANCE.DOCUMENTATION, ARCH.HERMES_ASTRA_BOUNDARY, DESIGN.GOVERNANCE.TASK_CONTRACT
> - **Status:** FROZEN

> 状态：`FROZEN / 2026-07-20`  
> 目标：让 Task authorization、Tool Gateway、ExternalOperation、idempotency、Approval、duplicate-side-effect Rule 和 reconciliation 使用同一套副作用身份。

## 1. Core invariant

Phase 3 的副作用身份链固定为：

```text
TaskContract.authorized_effect
→ raw tool request
→ versioned Effect Normalizer
→ CanonicalEffectRequest
→ effect_identity
→ Approval binding, if required
→ ExternalOperation
→ idempotency key
→ Receipt / Evidence / reconciliation
```

以下字段不能各自独立定义“是不是同一次副作用”：

```text
tool_name
tool_call_id
operation_type
request_hash
idempotency_key
approval token
provider operation ID
```

它们都必须引用或可验证地映射到唯一的 `effect_identity`。Tool call retry、Adapter retry、进程重启和 reconciliation 不得生成新的 effect identity。

## 2. CanonicalEffectRequest

所有 `resolved_tools.access_mode = effect` 的调用，在进入外部系统前必须被注册的 Effect Normalizer 转换为：

```text
effect_schema_version
normalizer_id / normalizer_version
task_contract_ref
effect_intent_ref
authority_domain
effect_type / effect_type_version
subject_ref
normalized_parameters
normalized_parameters_hash
effect_request_hash
effect_identity
```

示例：

```json
{
  "effect_schema_version": "1",
  "normalizer_id": "refund.request",
  "normalizer_version": "1",
  "task_contract_ref": {
    "contract_id": "contract-refund-order-o123",
    "contract_version": "1",
    "contract_hash": "sha256:..."
  },
  "effect_intent_ref": "refund-order-o123-once",
  "authority_domain": "payments.example/merchant-main",
  "effect_type": "finance.refund",
  "effect_type_version": "1",
  "subject_ref": {
    "authority_domain": "commerce.example",
    "type": "order",
    "id": "O123"
  },
  "normalized_parameters": {
    "amount_minor": 50000,
    "currency": "CNY",
    "destination": "original_payment_method"
  },
  "normalized_parameters_hash": "sha256:...",
  "effect_request_hash": "sha256:...",
  "effect_identity": "effect:sha256:..."
}
```

原始 Tool 参数和模型生成文本可以进入审计 Receipt，但不得直接参与 Core identity comparison。

## 3. Normalization requirements

Effect Normalizer 是业务 Adapter 的版本化纯函数：

```text
(Task Contract, matched effect intent, raw request)
→ CanonicalEffectRequest | deterministic rejection
```

Normalizer 必须：

- 不调用外部系统；
- 不修改 Task、Operation、Receipt 或 Fact；
- 固定 `normalizer_id/version`；
- 统一默认值、字段别名、货币单位、大小写、Unicode、时区、排序和可忽略传输字段；
- 移除 nonce、trace ID、tool call ID、时间戳等不属于业务意图的字段；
- 验证 request 与 Task Contract subject、parameter constraints 和 effect type 匹配；
- 对相同规范输入产生相同输出；
- 对未知或歧义语义 fail closed。

例如 `500.00 CNY` 与 `amount_minor=50000,currency=CNY` 应在同一 normalizer version 下归一为相同参数；`50000 CNY` 与 `80000 CNY` 必须产生不同 identity。

## 4. Hash construction

`normalized_parameters_hash`：

```text
sha256(JCS(normalized_parameters))
```

`effect_request_hash`：

```text
sha256(JCS({
  effect_schema_version,
  normalizer_id,
  normalizer_version,
  task_contract_ref,
  effect_intent_ref,
  authority_domain,
  effect_type,
  effect_type_version,
  subject_ref,
  normalized_parameters_hash
}))
```

第一版定义：

```text
effect_identity = "effect:" + effect_request_hash
```

因此 identity 同时绑定：

- Task Contract 的精确版本；
- Contract 中声明的 effect intent slot；
- 外部权威域；
- 业务 effect type/version；
- 业务对象；
- 规范化参数；
- normalizer version。

同一 Task Contract 中，如果业务上确实需要两个参数相同的副作用，必须声明两个不同的 `effect_intent_id`。随机改变参数、nonce 或 idempotency key 不能创建额外授权。

## 5. Relationship to idempotency

`effect_identity` 表示 Astra 的规范业务意图；`idempotency_key` 是提交给某个外部 authority/Adapter 的重放键。

规则：

```text
same effect_identity + same authority adapter generation
→ same idempotency key

different effect_identity
→ different idempotency key
```

推荐：

```text
idempotency_key = opaque_adapter_key(effect_identity, adapter_key_version)
```

可以直接使用 hash，也可以使用 HMAC/映射表满足外部格式或信息隐藏要求，但必须保存：

```text
effect_identity
idempotency_key
adapter_id / adapter_version
adapter_key_version
```

禁止用模型提供的任意字符串作为最终 idempotency key。模型可以提供业务参考值，但 Gateway/Adapter 必须从已验证的 effect identity 生成或查找正式 key。

## 6. ExternalOperation identity

每个有副作用的外部调用必须在调用前建立或复用：

```text
operation_id
task / attempt / execution refs
task_contract_ref
effect_identity UNIQUE within authority domain
effect_request_hash
effect_type / effect_type_version
subject_ref
idempotency_key
approval_ref, optional
external_operation_id, optional
status / version / timestamps
```

第一版 invariant：

```text
UNIQUE(authority_domain, effect_identity)
```

重复执行同一 effect request 返回已有 ExternalOperation，而不是创建第二个 operation。新的 Execution 或 Attempt 可以继续推进、查询或 reconcile 同一 operation，但不能把它伪装成新的业务副作用。

若一个外部系统需要多阶段 commit，业务 Adapter 可以维护阶段记录，但对 Task completion 有意义的业务 effect 仍须归属于同一个 effect identity，除非 Task Contract 明确声明多个独立 effect intents。

## 7. Operation state and crash windows

状态保持：

```text
prepared
in_flight
acknowledged
confirmed
indeterminate
failed
```

正式流程：

```text
normalize and authorize effect
→ verify exact Approval binding if required
→ persist prepared ExternalOperation
→ call authority with persisted idempotency key
→ persist acknowledged response
→ collect authoritative remote state
→ confirmed | failed | indeterminate
```

崩溃恢复必须先按 `effect_identity` 加载已有 operation，再使用 `idempotency_key`、`external_operation_id` 或业务状态查询进行 reconciliation。不得因为 Execution、Attempt、tool_call_id 或 provider request ID 改变而重新提交一个新 identity。

## 8. Duplicate-side-effect semantics

以下不算重复副作用：

- 相同 effect identity 的 Tool/Adapter retry；
- 相同 operation 的查询或 reconciliation；
- 同一 idempotency key 返回相同远端对象；
- response loss 后恢复确认已有远端结果。

以下构成 Finding 或 deterministic deny：

- 同一 effect intent 超过 `max_confirmed_occurrences`；
- 一个 effect identity 对应多个 confirmed remote operation；
- 同一 idempotency key 被映射到不同 effect identity；
- Approval 绑定 identity 与实际 operation identity 不同；
- 未匹配 authorized effect intent 的副作用请求；
- 通过改变非业务 nonce 或 tool call ID 规避 effect identity。

Duplicate Rule 使用 CanonicalEffectRequest 和 confirmed operation，不按 Tool 调用次数判断。

## 9. Reconciliation and evidence

Evidence Collector 固定以下引用：

```text
effect_identity
effect_request_hash
ExternalOperation ID/version/status
idempotency key hash or protected reference
external operation ID
business observation refs
approval ref, if required
```

强 completion evidence 必须确认 observation 与同一 authority domain、subject 和 effect identity 对应。若外部系统无法返回或推导对应关系，只能按 evaluator 语义形成 `unknown/indeterminate`，不得用 Hermes Tool Result 代替。

## 10. Contract/version changes

- normalizer version 是 identity 的一部分；旧 operation 永远按原 normalizer/version replay；
- Contract version 变化会产生新的 effect identity，即使参数相同；
- 新 Contract version 不自动允许重新执行旧版本已 confirmed 的业务效果；创建新版本时必须检查业务 effect continuity 和 occurrence limits；
- Adapter 升级不得重写历史 effect request 或 idempotency mapping；
- 无法兼容读取旧 normalizer/version 时，Task 进入 reconciliation/escalation，不能静默重新规范化。

## 11. Explicit non-goals

- effect identity 不决定业务下一步；
- effect identity 不替代 Completion Requirement；
- idempotency 不等于 confirmed；
- Receipt 不等于当前业务状态；
- 不要求外部系统参与 Astra 本地事务；
- 不建立跨所有企业系统的全局业务对象 ID；
- 不使用 Tool 名称、Tool call ID 或自然语言作为 canonical identity。

## 12. Contract tests

- 字段顺序、无意义空白、别名和等价单位不改变 identity；
- amount、currency、destination、subject、authority domain、effect intent 或 Contract version 变化会改变 identity；
- Tool retry、Execution resume、Attempt retry 和 response loss 不改变 identity/idempotency key；
- 相同 identity 只产生一个 ExternalOperation；
- 同一 Task 内两个有意相同副作用使用不同 effect intent，产生不同 identity；
- 模型提供的 idempotency key 不能覆盖正式 key；
- Approval identity 与 operation identity 不一致时 deny；
- confirmed operation 的 authoritative observation 可回链到同一 identity；
- 无 confirmation 能力时稳定落为 `indeterminate`；
- unknown normalizer/version fail closed。
