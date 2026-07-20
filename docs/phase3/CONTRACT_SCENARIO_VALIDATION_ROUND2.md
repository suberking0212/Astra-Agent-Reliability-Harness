# Phase 3 Contract Scenario Validation — Round 2

> 历史状态说明：本文记录的是 Runtime 实现开始前的契约冻结验收，因此文末
> `Phase 3 Implementation: NOT STARTED` 是报告当时的结论。Phase 3 后续生产
> 实现已完成并以 `phase3-governance-complete` 标签正式冻结；当前状态与验收
> 证据见 `docs/PHASE_STATUS.md` 和 `artifacts/phase3-governance-complete/`。

> 日期：2026-07-20  
> 状态：`PASSED / PHASE 3 DESIGN FREEZE APPROVED`  
> 范围：验证规范化 Task Contract、Canonical Effect Identity 和 exact Approval Binding 是否闭合 Round 1 的三个 Core 缺口。

## 1. Frozen dependency chain

Round 2 按以下顺序完成：

```text
Task Contract
→ CanonicalEffectRequest
→ effect_identity
→ exact Approval binding
→ ExternalOperation / idempotency
→ EvidenceSnapshot
→ RequirementEvaluation
→ PolicyDecision
→ Runtime CAS preconditions
```

规范文档：

- `TASK_CONTRACT.md`；
- `EFFECT_IDENTITY_AND_IDEMPOTENCY.md`；
- `APPROVAL_BINDING.md`。

## 2. Fixtures

Table-driven fixture：

```text
tests/fixtures/phase3_contract_round2.json
```

覆盖七个业务：

```text
customer_complaint
refund_approval
jira_issue
crm_customer_update
procurement_approval
email_send
browser_form_submit
```

每个业务覆盖五条路径：

```text
success
missing_input
approval_required
response_lost_indeterminate
stale_decision
```

总计 35 条端到端纯契约路径，不调用 Hermes、不执行真实业务 Tool、不实现 Phase 3 Runtime。

## 3. Verified invariants

契约测试验证：

- Task Contract 使用固定 envelope、version 和 content hash；
- effect Tool 必须匹配唯一 authorized effect intent；
- 同一 Contract/intent/subject/normalized parameters 产生稳定 effect identity；
- Contract version 变化会改变 effect identity；
- Approval requirement 引用 exact effect intent；
- Approval、ExternalOperation 和 EvidenceSnapshot 使用同一 effect identity/request hash；
- response loss 稳定产生 `unknown → reconcile`，不伪造成功；
- missing input 产生 `request_input`；
- missing approval 产生 `request_approval`；
- completion satisfied 产生 `complete`，但 stale Task version 被 CAS 拒绝；
- Contract 中不存在 next tool、ordered steps、workflow 或 business plan；
- Round 2 没有扩展 Task 状态、Policy Action、Completion Aggregator 或 Runtime CAS 模型。

## 4. Test results

专项测试：

```text
pytest -q tests/test_phase3_contract_round2.py
50 passed
```

其中包括：

```text
35 scenario/path contract-chain cases
7 Task Contract envelope/authorization cases
7 effect identity stability/version-binding cases
1 Core surface non-expansion case
```

全量回归：

```text
pytest -q
64 passed, 1 skipped
```

唯一 skip 为原有 opt-in live Provider smoke test，原因仍是未提供显式 Provider 凭证，与 Phase 3 contract freeze 无关。

静态检查：

```text
ruff check tests/test_phase3_contract_round2.py
All checks passed
```

## 5. Core surface audit

Round 2 未要求增加：

- Workflow 或 DAG；
- Planner Agent；
- 业务专用 Task 状态；
- 新的 Policy Action；
- Completion boolean DSL；
- 业务字段进入 Core Aggregator；
- Hermes 私有类型进入 Astra Core；
- Phase 3 Runtime 实现。

固定 Task 状态仍为：

```text
pending
running
waiting_input
waiting_approval
reconciling
succeeded
failed
cancelled
```

固定 Policy Action 仍为：

```text
complete
continue_with_feedback
start_new_attempt
request_input
request_approval
reconcile
fail
escalate
```

业务差异仍只进入 Plugin/MCP、Tool Adapter、Effect Normalizer、Evidence Collector、Constraint handler 和 versioned Requirement Evaluator。

## 6. Freeze decision

```text
Overall architecture: PASS
Business extension model: PASS
Completion / Evidence / Policy: PASS
Hermes / Astra boundary: PASS
Normalized Task Contract: PASS
Canonical effect identity: PASS
Exact Approval binding: PASS
Seven-scenario Round 2 fixtures: PASS
Core surface unchanged: PASS
Phase 3 Design: FROZEN
Phase 3 Implementation: NOT STARTED
```

Round 1 的三个 Core 阻塞项已经关闭。后续实现若要求修改这些 frozen contracts，必须通过新的 ADR/contract version 和同等级场景回归，而不能作为普通实现细节静默改变。
