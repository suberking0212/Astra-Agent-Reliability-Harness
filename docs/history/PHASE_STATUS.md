# Astra × Hermes 阶段状态

> **Historical notice:** Superseded as the current implementation-status source
> by [docs/STATUS.md](../STATUS.md). This file retains its original status
> report and claims for audit provenance only; it is not current implementation
> authority.

> **Documentation Governance**
> - **Role:** Historical implementation-status register.
> - **Authority:** Informational status — non-normative.
> - **Scope:** Current completion and validation claims, with explicit exclusions.
> - **Not Responsible For:** Product requirements, architecture, Runtime Design, remediation scope, or acceptance criteria.
> - **Depends On:** GOVERNANCE.DOCUMENTATION
> - **Status:** HISTORICAL REFERENCE; superseded by `docs/STATUS.md`.

> 更新日期：2026-08-06

Status labels such as `COMPLETE`, `IMPLEMENTED`, `FROZEN`, or `LOCALLY
VALIDATED` are implementation-reporting terms only. They neither approve a
release nor define, alter, or supersede any registered Topic Authority.

## 当前状态

```text
Phase 1: COMPLETE
Phase 2 Core: IMPLEMENTED / LOCALLY VALIDATED
Phase 2 Live Provider Integration: PENDING
Phase 3 P0 Production Governance Chain: LOCALLY VALIDATED
Phase 4 Critical Durable Runtime Paths: LOCALLY VALIDATED
Hermes Learning Integration Foundation: LOCALLY VALIDATED
Full Batch / Stress / Production Readiness: NOT CLAIMED
```

已完成 Phase 只保留实现结论、架构边界和仍然有效的能力说明。历史验收条目、
Gate、测试结果、测试命令、冻结基线和验收产物不再作为项目状态的一部分保留，
也不得作为后续阶段的阻塞前置条件。

## 已验证能力

Phase 1 明确了 Hermes 的主执行链、模块职责和 Astra 可使用的稳定执行边界。
Phase 2 完成了首条投诉处理纵向闭环，包括 Hermes Executor Adapter、固定 Bridge
Plugin、受控 Tool Gateway、Mock Business Service、结果提交、持久化交互、预算
记录和 Neutral Trace。

Phase 3/4 当前只声明 P0 主链已经通过本地 Production composition 验证：Task
Contract、Canonical Effect Identity、Approval Binding、Task/Attempt/Execution
生命周期、EvidenceSnapshot、Requirement Evaluator、Task Rules、Task Policy、
Decision Context、CAS 和 Runtime Governance Core 已串入同一生产治理链：

```text
Phase2Runtime
→ RuntimeGovernanceCore
→ AstraStore
```

该实现没有创建平行 Runtime、Workflow Engine、Coordinator、Dispatcher 或第二套
Agent Loop。

Hermes Learning Integration Foundation 当前只实现并通过验证 content-free evidence
association：将 Hermes Artifact reference 与 Task、Attempt、Execution、profile、
session、validation、安全和 provenance 事实关联；它不保存 Artifact 内容或原生
生命周期状态。

该 Foundation 不调度、配置或调用 Hermes Curator，也不修改 Hermes 私有接口；不实现
Admission Decision、Artifact lifecycle governance、Astra-managed Memory / Skill
system 或 Complete Learning Governance。这些能力不属于当前完成声明。

## 持续有效的职责边界

> Hermes 负责 Agent Execution Logic；Astra 负责任务事实、Task Execution
> Lifecycle 和 execution-boundary governance。

- Hermes 负责推理、工具选择、参数生成、执行顺序、局部错误适应和动态重规划。
- Astra Task Runtime 负责持久化、等待、恢复、取消、预算、终态和跨进程生命周期。
- RuntimeGovernanceCore 负责权威证据、完成判定、Task Policy 和 CAS 状态应用。
- Tool Gateway 负责确定性权限、审批、Schema、幂等和副作用约束。

## 当前冻结范围

当前冻结在以下关键路径：Hermes waiting/resume/restart、Governance
`REQUEST_APPROVAL`、副作用 fail-closed 与幂等、取消迟到 Worker 保护、
`start_new_attempt` 原子性，以及 Production Snapshot 收集、持久化和复用。

Execution-type Runtime 整改已落实到当前工作树：只接受
`direct_response`、`tool_execution`、`interaction_required`、
`approval_required`；CLI 使用显式 `/ask` 与 `/task`；Interaction 具有明确
clarification/approval purpose；所有新 RuntimeInvocation 经过统一 Contract
Preflight/Catalog 校验；旧 `conversation` Contract 在迁移时以
`contract_runtime_incompatible` 终止且不改写历史 Contract；订单域新增受控读取工具
`list_customer_orders(customer_id)`。当前证据为对应的轻量 Contract、migration、
Interaction resume 与 Gateway receipt 定向测试，不包含全量或 live Provider 验收。

每个 authoritative version 的独立 E2E、双 Runtime 并发 apply、全量历史测试迁移、
大规模 stress，以及 live Provider 业务验收不在本轮完成声明中。缺少显式 live
Provider 凭据时，不得把本地脚本化 Provider 结果写成线上生产验收通过。
