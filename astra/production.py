"""Production composition root and long-lived Runtime entry points."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .budget import BudgetLedger
from .business import (
    BusinessSandboxConfig,
    BusinessService,
    HttpBusinessSandboxAdapter,
)
from .complaint_extension import build_complaint_extension
from .domain_extension import DomainExtension
from .domain import AgentExecutor, ExecutionEventSink
from .execution_profiles import get_execution_profile
from .observations import ExternalOperationConfirmation
from .phase3.completion import (
    AuthorizedEffectConfirmedEvaluator,
    CompletionContract,
    DirectResponseEvidenceEvaluator,
    RequirementEvaluatorRegistry,
)
from .phase3.effects import (
    CanonicalEffectRequest,
    EffectNormalizerRegistry,
    ExternalOperation,
)
from .phase3.governance import GovernanceStore, RuntimeGovernanceCore
from .phase3.task_contract import TaskContract
from .phase3.task_rules import (
    TaskRuleRegistry,
    build_production_task_rule_registry,
)
from .result_validator import (
    MinimalResultValidator,
    ResultEvaluatorRegistry,
    ResultValidator,
)
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
    ConstraintHandlerRegistry,
)
from .trace import NeutralTraceCollector



@dataclass(frozen=True)
class ProductionConfig:
    """Configuration for one long-lived Astra production process."""

    database_path: str | Path
    business_sandbox_config: BusinessSandboxConfig | None = None
    worker_poll_interval: float = 0.25
    lease_duration_seconds: float = 30.0
    heartbeat_interval_seconds: float = 10.0
    execution_profile: str = "astra_controlled"
    hermes_home: str | Path | None = None

    def __post_init__(self) -> None:
        if self.worker_poll_interval <= 0:
            raise ValueError("worker_poll_interval must be positive")
        if self.lease_duration_seconds <= 0:
            raise ValueError("lease_duration_seconds must be positive")
        if (
            self.heartbeat_interval_seconds <= 0
            or self.heartbeat_interval_seconds >= self.lease_duration_seconds
        ):
            raise ValueError(
                "heartbeat_interval_seconds must be positive and shorter than lease"
            )
        profile = get_execution_profile(self.execution_profile)
        if not profile.astra_governed:
            raise ValueError("ProductionRuntime requires an Astra-governed profile")
        if self.hermes_home is None:
            database = Path(self.database_path)
            derived = (
                Path.cwd() / "var" / "hermes-home"
                if str(database) == ":memory:"
                else database.parent / (database.stem + "-hermes-home")
            )
            object.__setattr__(self, "hermes_home", derived)


@dataclass(frozen=True)
class ExecutorDependencies:
    """Authoritative dependencies exposed to an AgentExecutor factory."""

    store: AstraStore
    runtime: TaskRuntime
    business: BusinessService | None
    gateway: AstraToolGateway
    validator: ResultValidator
    budget: BudgetLedger
    trace: NeutralTraceCollector


ExecutorFactory = Callable[[ExecutorDependencies], AgentExecutor]
BusinessFactory = Callable[[AstraStore], BusinessService]
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
        business_factory: BusinessFactory | None = None,
        domain_extensions: Sequence[DomainExtension] | None = None,
        worker_fault_injector: (
            Callable[[str, Mapping[str, Any]], None] | None
        ) = None,
    ) -> None:
        self.config = config
        self.execution_profile = get_execution_profile(config.execution_profile)
        if (
            business_factory is None
            and config.business_sandbox_config is None
            and domain_extensions is None
        ):
            raise ValueError(
                "business sandbox configuration is required; "
                "MockBusinessService is not a production fallback"
            )
        self.store = AstraStore(config.database_path)
        try:
            self.evaluator_registry = RequirementEvaluatorRegistry()
            self.evaluator_registry.register(AuthorizedEffectConfirmedEvaluator())
            self.evaluator_registry.register(DirectResponseEvidenceEvaluator())
            self.normalizer_registry = EffectNormalizerRegistry()
            self.constraint_registry = ConstraintHandlerRegistry()
            self.result_evaluator_registry = ResultEvaluatorRegistry()
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
            if business_factory is not None:
                self.business = business_factory(self.store)
            elif config.business_sandbox_config is not None:
                sandbox_config = config.business_sandbox_config
                self.business = HttpBusinessSandboxAdapter(sandbox_config)
            else:
                self.business = None
            extensions = tuple(domain_extensions or ())
            if not extensions:
                if self.business is None:
                    raise ValueError("at least one domain extension is required")
                extensions = (build_complaint_extension(self.business),)
            tool_definitions = {}
            for extension in extensions:
                for normalizer in extension.normalizers:
                    self.normalizer_registry.register(normalizer)
                for constraint in extension.constraints:
                    self.constraint_registry.register(constraint)
                for evaluator in extension.result_evaluators:
                    self.result_evaluator_registry.register(evaluator)
                materialized = extension.materialize_tool_definitions(
                    self.store, self.governance
                )
                overlap = set(tool_definitions).intersection(materialized)
                if overlap:
                    raise ValueError(
                        f"duplicate production tool definitions: {sorted(overlap)!r}"
                    )
                tool_definitions.update(materialized)
            self.domain_extensions = extensions
            self.gateway = AstraToolGateway(
                self.store,
                self.business,
                governance_core=self.governance,
                normalizer_registry=self.normalizer_registry,
                constraint_registry=self.constraint_registry,
                tool_definitions=tool_definitions,
            )
            self.validator = MinimalResultValidator(
                self.store, self.result_evaluator_registry
            )
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
                raise ValueError(
                    "Astra Runtime requires a public Hermes execution adapter; "
                    "Hermes does not currently expose one."
                )
            self.executor = executor_factory(dependencies)
            self.worker = SingleWorker(
                self.runtime,
                self.executor,
                lease_duration_seconds=config.lease_duration_seconds,
                heartbeat_interval_seconds=config.heartbeat_interval_seconds,
                operation_observer=self._observe_external_operation,
                fault_injector=worker_fault_injector,
                execution_profile=self.execution_profile.profile_id,
                execution_profile_version=self.execution_profile.profile_version,
                execution_profile_hash=self.execution_profile.profile_hash,
                hermes_home=str(config.hermes_home),
            )
            self.startup_recovery = self.runtime.startup_recover(
                self._observe_external_operation,
                lease_owner_id=self.worker.worker_id,
                lease_duration_seconds=config.lease_duration_seconds,
            )
            self._assert_single_authority()
        except BaseException:
            self.store.close()
            raise
        self._closed = False

    def _observe_external_operation(
        self,
        operation: ExternalOperation,
        canonical: CanonicalEffectRequest,
    ) -> ExternalOperationConfirmation:
        """Read the configured business sandbox without replaying the effect."""

        return self.gateway.observe_external_operation(operation, canonical)

    def _assert_single_authority(self) -> None:
        stores = (
            self.runtime.store,
            self.runtime.governance_store.authority_store,
            self.gateway.store,
            self.validator.store,
            self.budget.store,
            self.trace.store,
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
        if self.validator.evaluator_registry is not self.result_evaluator_registry:
            raise RuntimeError(
                "Validator does not use the production Result Evaluator registry"
            )
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
            "result_evaluators": self.result_evaluator_registry.registered_refs,
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
        conversation_id: str | None = None,
        turn_id: str | None = None,
        message_id: str | None = None,
        message_hash: str | None = None,
    ) -> TaskSubmissionResult:
        return self.runtime.submit_task(
            command_id=command_id,
            task_id=task_id,
            contract=contract,
            completion_contract=completion_contract,
            priority=priority,
            ready_at=ready_at,
            conversation_id=conversation_id,
            turn_id=turn_id,
            message_id=message_id,
            message_hash=message_hash,
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
        reconciliations = rows(
            """
            SELECT * FROM phase3_reconciliations
            WHERE task_id = ? ORDER BY created_at, reconciliation_id
            """
        )
        checkpoints = rows(
            """
            SELECT checkpoint_id, attempt_id, execution_id, run_request_id,
                   boundary, task_version, attempt_version, authority_hash,
                   envelope_json, created_at
            FROM phase4_checkpoints
            WHERE task_id = ? ORDER BY created_at, checkpoint_id
            """
        )
        completion_validations = rows(
            """
            SELECT completion_validation_id, status, validation_json, created_at
            FROM phase3_completion_validations
            WHERE task_id = ? ORDER BY created_at, completion_validation_id
            """
        )
        policy_decisions = rows(
            """
            SELECT decision_id, decision_key, attempt_id, application_status,
                   decision_json, created_at, applied_at, derived_record_id
            FROM phase3_policy_decisions
            WHERE task_id = ? ORDER BY created_at, decision_id
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
        outbox = rows(
            """
            SELECT outbox.outbox_id, outbox.fact_id, outbox.topic,
                   outbox.delivery_status, outbox.delivery_attempts,
                   outbox.created_at
            FROM phase3_outbox AS outbox
            JOIN phase3_reliability_facts AS fact
              ON fact.fact_id = outbox.fact_id
            WHERE fact.task_id = ?
            ORDER BY outbox.created_at, outbox.outbox_id
            """
        )
        decode(run_requests, "feedback_json")
        decode(interactions, "payload_json", "resolution_json")
        decode(approval_requests, "request_json")
        decode(approval_resolutions, "resolution_json")
        decode(operations, "operation_json")
        decode(receipts, "request_json", "result_json")
        decode(observations, "observation_json")
        decode(reconciliations, "result_json", "last_error_json")
        decode(checkpoints, "envelope_json")
        decode(completion_validations, "validation_json")
        decode(policy_decisions, "decision_json")
        decode(rule_evaluations, "evaluation_json")
        decode(reliability_facts, "fact_json")

        business_objects = [
            dict(
                observation["observation_json"].get(
                    "payload", observation["observation_json"].get("state", {})
                )
            )
            for observation in observations
        ]

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
            "reconciliations": reconciliations,
            "checkpoints": checkpoints,
            "evidence_snapshots": snapshots,
            "completion_validations": completion_validations,
            "policy_decisions": policy_decisions,
            "rule_evaluations": rule_evaluations,
            "reliability_facts": reliability_facts,
            "outbox": outbox,
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

    async def run_worker_once(
        self,
        *,
        run_request_id: str | None = None,
        event_sink: ExecutionEventSink | None = None,
    ) -> SingleWorkerRunResult | None:
        result = await self.worker.run_once(
            run_request_id=run_request_id,
            event_sink=event_sink,
        )
        return result

    async def run_worker(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        on_result: WorkerResultCallback | None = None,
        event_sink: ExecutionEventSink | None = None,
    ) -> None:
        """Continuously consume durable Run Requests until explicitly stopped."""

        while stop_event is None or not stop_event.is_set():
            result = await self.run_worker_once(event_sink=event_sink)
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

    async def aclose(self) -> None:
        if self._closed:
            return
        self.store.close()
        self._closed = True

    def close(self) -> None:
        if not self._closed:
            self.store.close()
            self._closed = True

    def __enter__(self) -> "ProductionRuntime":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
