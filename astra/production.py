"""Production composition root and long-lived Runtime entry points."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .budget import BudgetLedger
from .domain import AgentExecutor
from .hermes_adapter.executor import HermesExecutor
from .mock_business import MockBusinessService
from .phase3.completion import (
    AuthorizedEffectConfirmedEvaluator,
    CompletionContract,
    RequirementEvaluatorRegistry,
)
from .phase3.effects import EffectNormalizerRegistry
from .phase3.governance import GovernanceStore, RuntimeGovernanceCore
from .phase3.task_contract import TaskContract
from .phase3.task_rules import (
    TaskRuleRegistry,
    build_production_task_rule_registry,
)
from .result_validator import MinimalResultValidator
from .runtime import (
    InteractionResolutionResult,
    SingleWorker,
    SingleWorkerRunResult,
    TaskCancellationResult,
    TaskRuntime,
    TaskSubmissionResult,
)
from .storage import AstraStore
from .tool_gateway import (
    AstraToolGateway,
    ComplaintOrderScopeConstraint,
    ComplaintTicketNormalizer,
    ConstraintHandlerRegistry,
    ExactParameterConstraint,
)
from .trace import NeutralTraceCollector


@dataclass(frozen=True)
class ProductionConfig:
    """Configuration for one long-lived Astra production process."""

    database_path: str | Path
    hermes_root: str | Path
    provider_config: Mapping[str, Any] = field(default_factory=dict)
    worker_poll_interval: float = 0.25
    hermes_session_database_path: str | Path | None = None

    def __post_init__(self) -> None:
        if self.worker_poll_interval <= 0:
            raise ValueError("worker_poll_interval must be positive")


@dataclass(frozen=True)
class ExecutorDependencies:
    """Authoritative dependencies exposed to an AgentExecutor factory."""

    store: AstraStore
    runtime: TaskRuntime
    business: MockBusinessService
    gateway: AstraToolGateway
    validator: MinimalResultValidator
    budget: BudgetLedger
    trace: NeutralTraceCollector


ExecutorFactory = Callable[[ExecutorDependencies], AgentExecutor]
BusinessFactory = Callable[[AstraStore], MockBusinessService]
WorkerResultCallback = Callable[[SingleWorkerRunResult], None]


class ProductionRuntime:
    """The sole composition root for a deployed Astra Runtime process.

    Every stateful component is constructed from one ``AstraStore`` and one
    ``TaskRuntime``.  Executor factories may replace the provider-facing port,
    but cannot supply another Runtime or Store authority.
    """

    def __init__(
        self,
        config: ProductionConfig,
        *,
        executor_factory: ExecutorFactory | None = None,
        hermes_client_factory: Callable[[Any], Any] | None = None,
        business_factory: BusinessFactory | None = None,
        worker_fault_injector: (
            Callable[[str, Mapping[str, Any]], None] | None
        ) = None,
    ) -> None:
        self.config = config
        self.store = AstraStore(config.database_path)
        try:
            self.evaluator_registry = RequirementEvaluatorRegistry()
            self.evaluator_registry.register(AuthorizedEffectConfirmedEvaluator())
            self.normalizer_registry = EffectNormalizerRegistry()
            self.normalizer_registry.register(ComplaintTicketNormalizer())
            self.constraint_registry = ConstraintHandlerRegistry()
            self.constraint_registry.register(ComplaintOrderScopeConstraint())
            self.constraint_registry.register(ExactParameterConstraint())
            self.task_rule_registry: TaskRuleRegistry = (
                build_production_task_rule_registry()
            )
            governance_store = GovernanceStore.from_astra_store(self.store)
            governance_core = RuntimeGovernanceCore(
                governance_store,
                evaluator_registry=self.evaluator_registry,
                task_rule_registry=self.task_rule_registry,
            )
            self.runtime = TaskRuntime(
                self.store,
                governance_core=governance_core,
            )
            self.governance = self.runtime.governance_core
            self.startup_recovery = self.runtime.recover_pending_policy_decisions()
            self.business = (business_factory or MockBusinessService)(self.store)
            self.gateway = AstraToolGateway(
                self.store,
                self.business,
                governance_core=self.governance,
                normalizer_registry=self.normalizer_registry,
                constraint_registry=self.constraint_registry,
            )
            self.validator = MinimalResultValidator(self.store, self.business)
            self.budget = BudgetLedger(self.store)
            self.trace = NeutralTraceCollector(self.store)
            dependencies = ExecutorDependencies(
                store=self.store,
                runtime=self.runtime,
                business=self.business,
                gateway=self.gateway,
                validator=self.validator,
                budget=self.budget,
                trace=self.trace,
            )
            if executor_factory is None:
                self.executor: AgentExecutor = HermesExecutor(
                    hermes_root=config.hermes_root,
                    store=self.store,
                    gateway=self.gateway,
                    validator=self.validator,
                    budget=self.budget,
                    trace=self.trace,
                    runtime=self.runtime,
                    client_factory=hermes_client_factory,
                    session_database_path=config.hermes_session_database_path,
                )
            else:
                self.executor = executor_factory(dependencies)
            self.worker = SingleWorker(
                self.runtime,
                self.executor,
                provider_config=config.provider_config,
                fault_injector=worker_fault_injector,
            )
            self._assert_single_authority()
        except BaseException:
            self.store.close()
            raise
        self._closed = False

    def _assert_single_authority(self) -> None:
        stores = (
            self.runtime.store,
            self.runtime.governance_store.authority_store,
            self.gateway.store,
            self.validator.store,
            self.budget.store,
            self.trace.store,
            self.business.store,
        )
        if any(store is not self.store for store in stores):
            raise RuntimeError("Production components do not share AstraStore")
        if self.runtime.governance_store.connection is not self.store.connection:
            raise RuntimeError("Governance does not share the Runtime connection")
        if self.gateway.governance_core is not self.governance:
            raise RuntimeError("Gateway does not share production Governance")
        if self.worker.runtime is not self.runtime:
            raise RuntimeError("Worker does not share the production TaskRuntime")
        if self.governance.evaluator_registry is not self.evaluator_registry:
            raise RuntimeError("Governance does not use the production Evaluator registry")
        if self.governance.task_rule_registry is not self.task_rule_registry:
            raise RuntimeError("Governance does not use the production Task Rule registry")
        if self.gateway.normalizer_registry is not self.normalizer_registry:
            raise RuntimeError("Gateway does not use the production Normalizer registry")
        if self.gateway.constraint_registry is not self.constraint_registry:
            raise RuntimeError("Gateway does not use the production Constraint registry")
        executor_runtime = getattr(self.executor, "runtime", self.runtime)
        if executor_runtime is not self.runtime:
            raise RuntimeError("Executor does not share the production TaskRuntime")

    @property
    def authority_identity(self) -> Mapping[str, int]:
        """Expose process-local identity evidence for diagnostics and tests."""

        return {
            "store": id(self.store),
            "connection": id(self.store.connection),
            "runtime": id(self.runtime),
            "governance_store": id(self.runtime.governance_store),
            "worker_runtime": id(self.worker.runtime),
            "executor_runtime": id(getattr(self.executor, "runtime", self.runtime)),
        }

    @property
    def component_registry_manifest(
        self,
    ) -> Mapping[str, tuple[tuple[str, str], ...]]:
        """Expose the immutable production component/version composition."""

        return {
            "evaluators": self.evaluator_registry.registered_refs,
            "normalizers": self.normalizer_registry.registered_refs,
            "constraints": self.constraint_registry.registered_refs,
            "task_rules": self.task_rule_registry.registered_refs,
        }

    def submit_task(
        self,
        *,
        command_id: str,
        task_id: str,
        contract: TaskContract,
        completion_contract: CompletionContract | None = None,
        priority: int = 0,
        ready_at: str | None = None,
    ) -> TaskSubmissionResult:
        return self.runtime.submit_task(
            command_id=command_id,
            task_id=task_id,
            contract=contract,
            completion_contract=completion_contract,
            priority=priority,
            ready_at=ready_at,
        )

    def resolve_interaction(
        self,
        *,
        command_id: str,
        interaction_id: str,
        expected_version: int,
        resolution: Mapping[str, Any],
        priority: int = 0,
        ready_at: str | None = None,
    ) -> InteractionResolutionResult:
        return self.runtime.resolve_interaction(
            command_id=command_id,
            interaction_id=interaction_id,
            expected_version=expected_version,
            resolution=resolution,
            priority=priority,
            ready_at=ready_at,
        )

    def task_status(self, task_id: str) -> Mapping[str, Any]:
        """Return one read-only, cross-authority view for operator black-box use."""

        task = self.store.query_one(
            "SELECT * FROM phase3_tasks WHERE task_id = ?", (task_id,)
        )
        if task is None:
            raise KeyError(task_id)

        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(row) for row in self.store.query_all(sql, (task_id,))]

        def decode(records: list[dict[str, Any]], *columns: str) -> None:
            for record in records:
                for column in columns:
                    value = record.get(column)
                    if isinstance(value, str):
                        record[column] = json.loads(value)

        attempts = rows(
            "SELECT * FROM phase3_attempts WHERE task_id = ? ORDER BY ordinal"
        )
        executions = rows(
            "SELECT * FROM executions WHERE task_id = ? ORDER BY started_at"
        )
        run_requests = rows(
            """
            SELECT * FROM phase4_run_requests
            WHERE task_id = ? ORDER BY created_at, run_request_id
            """
        )
        interactions = rows(
            """
            SELECT * FROM interactions
            WHERE task_id = ? ORDER BY created_at, interaction_id
            """
        )
        approval_requests = rows(
            """
            SELECT * FROM phase3_approval_requests
            WHERE task_id = ? ORDER BY created_at, approval_request_id
            """
        )
        approval_resolutions = rows(
            """
            SELECT * FROM phase3_approval_resolutions
            WHERE task_id = ? ORDER BY created_at, approval_resolution_id
            """
        )
        operations = rows(
            """
            SELECT * FROM phase3_external_operations
            WHERE task_id = ? ORDER BY created_at, operation_id
            """
        )
        receipts = rows(
            """
            SELECT * FROM execution_receipts
            WHERE task_id = ? ORDER BY created_at, receipt_id
            """
        )
        observations = rows(
            """
            SELECT * FROM phase3_business_observations
            WHERE task_id = ? ORDER BY recorded_at, observation_id
            """
        )
        snapshots = rows(
            """
            SELECT evidence_snapshot_id, attempt_id, collection_trigger_id,
                   authoritative_versions_hash, collector_version,
                   content_hash, created_at
            FROM phase3_evidence_snapshots
            WHERE task_id = ? ORDER BY created_at, evidence_snapshot_id
            """
        )
        rule_evaluations = rows(
            """
            SELECT rule_evaluation_id, rule_id, rule_version, decision_point,
                   evidence_snapshot_id, status, evaluation_json, created_at
            FROM phase3_rule_evaluations
            WHERE task_id = ? ORDER BY created_at, rule_evaluation_id
            """
        )
        reliability_facts = rows(
            """
            SELECT sequence, fact_id, fact_type, attempt_id, source_kind,
                   source_name, source_event_id, fact_json, recorded_at
            FROM phase3_reliability_facts
            WHERE task_id = ? ORDER BY sequence
            """
        )
        decode(run_requests, "feedback_json")
        decode(interactions, "payload_json", "resolution_json")
        decode(approval_requests, "request_json")
        decode(approval_resolutions, "resolution_json")
        decode(operations, "operation_json")
        decode(receipts, "request_json", "result_json")
        decode(observations, "observation_json")
        decode(rule_evaluations, "evaluation_json")
        decode(reliability_facts, "fact_json")

        business_objects: list[Mapping[str, Any]] = []
        for operation in operations:
            external_id = operation["operation_json"].get("external_operation_id")
            if external_id is None:
                continue
            business_object = self.business.get_complaint_ticket(str(external_id))
            if business_object is not None:
                business_objects.append(dict(business_object))

        return {
            "task": {
                key: value
                for key, value in dict(task).items()
                if key != "contract_json"
            },
            "attempts": attempts,
            "executions": executions,
            "run_requests": run_requests,
            "interactions": interactions,
            "approval_requests": approval_requests,
            "approval_resolutions": approval_resolutions,
            "external_operations": operations,
            "receipts": receipts,
            "observations": observations,
            "evidence_snapshots": snapshots,
            "rule_evaluations": rule_evaluations,
            "reliability_facts": reliability_facts,
            "business_objects": business_objects,
        }

    async def cancel_task(
        self,
        *,
        command_id: str,
        task_id: str,
        expected_task_version: int,
        reason: str = "operator_cancelled",
    ) -> TaskCancellationResult:
        result = self.runtime.cancel_task(
            command_id=command_id,
            task_id=task_id,
            expected_task_version=expected_task_version,
            reason=reason,
        )
        for execution_id in result.active_execution_ids:
            try:
                await self.executor.cancel(execution_id, reason)
            except BaseException:
                # Cancel authority is already committed. Executor notification
                # is deliberately best effort and cannot roll it back.
                continue
        return result

    async def run_worker_once(self) -> SingleWorkerRunResult | None:
        return await self.worker.run_once()

    async def run_worker(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        on_result: WorkerResultCallback | None = None,
    ) -> None:
        """Continuously consume durable Run Requests until explicitly stopped."""

        while stop_event is None or not stop_event.is_set():
            result = await self.run_worker_once()
            if result is not None:
                if on_result is not None:
                    on_result(result)
                continue
            if stop_event is None:
                await asyncio.sleep(self.config.worker_poll_interval)
                continue
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.config.worker_poll_interval
                )
            except TimeoutError:
                pass

    def close(self) -> None:
        if not self._closed:
            self.store.close()
            self._closed = True

    def __enter__(self) -> "ProductionRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
