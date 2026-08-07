"""Runtime Governance Core required by the frozen Phase 3 contracts.

This callable component is composed into the existing Phase 2 Runtime. It
evaluates authoritative results and persists immutable PolicyDecisions, but it
does not apply Runtime lifecycle changes, form a parallel Runtime, select tools,
schedule business steps, retry Hermes calls, or encode a workflow.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import Field

from ..domain import ExecutionResult
from ..dynamic_authority import effective_contract
from .approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResolution,
    verify_approval_binding,
)
from .canonical import sha256_digest
from .completion import (
    BusinessObservation,
    CompletionContract,
    CompletionValidationResult,
    EvidenceSnapshot,
    RequirementEvaluation,
    RequirementEvaluatorRegistry,
    SubmittedResult,
    aggregate_completion,
    build_evidence_snapshot_identity,
)
from .effects import (
    CanonicalEffectRequest,
    ExternalOperation,
    ExternalOperationStatus,
)
from .facts import FactSource, OutboxMessage, ReliabilityFact
from .policy import (
    DecisionApplication,
    DecisionContext,
    DecisionPoint,
    InteractionSpec,
    PolicyAction,
    PolicyDecision,
    PolicyFeedback,
    TaskState,
    choose_policy_action,
    make_policy_decision,
)
from .task_contract import ExecutionType, FrozenContractModel, SubjectRef, TaskContract
from .task_rules import RuleEvaluation, TaskRuleContext, TaskRuleRegistry

if TYPE_CHECKING:
    from ..storage import AstraStore
    from .round2 import Round2ValidationReport


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _json(value: Any) -> str:
    if isinstance(value, FrozenContractModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


class AttemptState(str, Enum):
    ACTIVE = "active"
    WAITING = "waiting"
    RECONCILING = "reconciling"
    COMPLETED = "completed"
    FAILED = "failed"
    EXHAUSTED = "exhausted"
    SUPERSEDED = "superseded"
    CANCELLED = "cancelled"


class GovernanceApplicationResult(FrozenContractModel):
    decision_id: str
    application: DecisionApplication
    task_id: str
    task_state: TaskState
    task_version: int = Field(ge=0)
    attempt_id: str
    attempt_state: AttemptState
    attempt_version: int = Field(ge=0)
    derived_record_id: str | None = None


class GovernanceEvaluationResult(FrozenContractModel):
    execution_id: str
    evidence_snapshot: EvidenceSnapshot
    requirement_evaluations: tuple[RequirementEvaluation, ...]
    rule_evaluations: tuple[RuleEvaluation, ...]
    completion_validation: CompletionValidationResult
    policy_decision: PolicyDecision


class GovernanceEvaluationBlocked(RuntimeError):
    """Governance cannot safely materialize a frozen policy decision."""

    def __init__(self, code: str, execution_id: str) -> None:
        self.code = code
        self.execution_id = execution_id
        super().__init__(f"{code}: execution_id {execution_id!r}")


class GovernanceStore:
    """Governance access bound to the single authoritative AstraStore."""

    if TYPE_CHECKING:
        _astra_store: AstraStore

    def __init__(self, path: str | Path = ":memory:") -> None:
        raise RuntimeError(
            "GovernanceStore must be created with from_astra_store(AstraStore)"
        )

    @classmethod
    def from_astra_store(cls, store: "AstraStore") -> "GovernanceStore":
        """Bind governance tables and transactions to an existing Phase 2 store."""

        instance = cls.__new__(cls)
        instance._astra_store = store
        instance._initialize()
        return instance

    def close(self) -> None:
        return None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.authority_store.transaction() as connection:
            yield connection

    def _initialize(self) -> None:
        required = {
            "phase3_tasks",
            "phase3_attempts",
            "phase3_reconciliations",
            "phase3_external_operations",
            "phase3_approval_requests",
            "phase3_approval_resolutions",
            "phase3_completion_validations",
            "phase3_completion_contracts",
            "phase3_business_observations",
            "phase3_evidence_snapshots",
            "phase3_requirement_evaluations",
            "phase3_rule_evaluations",
            "phase3_policy_decisions",
            "phase3_reliability_facts",
            "phase3_outbox",
        }
        objects = {
            str(row[0]): str(row[1])
            for row in self.connection.execute(
                "SELECT name, type FROM sqlite_master WHERE name LIKE 'phase3_%'"
            )
        }
        missing = sorted(required.difference(objects))
        if missing:
            raise RuntimeError(
                "AstraStore schema is missing governance records: "
                + ", ".join(missing)
            )
        for compatibility_view in ("phase3_executions", "phase3_interactions"):
            if objects.get(compatibility_view) != "view":
                raise RuntimeError(
                    f"{compatibility_view} must be an AstraStore compatibility view"
                )

    def query_one(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> sqlite3.Row | None:
        return self.authority_store.query_one(sql, parameters)

    def query_all(
        self, sql: str, parameters: tuple[Any, ...] = ()
    ) -> list[sqlite3.Row]:
        return self.authority_store.query_all(sql, parameters)

    @property
    def connection(self) -> sqlite3.Connection:
        return self.authority_store.connection

    @property
    def authority_store(self) -> "AstraStore":
        assert self._astra_store is not None
        return self._astra_store


class RuntimeGovernanceCore:
    """Evaluate governed outcomes without advancing Runtime lifecycle state."""

    def __init__(
        self,
        store: GovernanceStore,
        *,
        evaluator_registry: RequirementEvaluatorRegistry | None = None,
        task_rule_registry: TaskRuleRegistry | None = None,
    ) -> None:
        self.store = store
        self.evaluator_registry = evaluator_registry or RequirementEvaluatorRegistry()
        self.task_rule_registry = task_rule_registry or TaskRuleRegistry()

    def register_completion_contract(self, contract: CompletionContract) -> None:
        """Persist one immutable, versioned Completion Contract."""

        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO phase3_completion_contracts(
                    contract_id, contract_version, task_type,
                    contract_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(contract_id, contract_version) DO NOTHING
                """,
                (
                    contract.contract_id,
                    contract.contract_version,
                    contract.task_type,
                    contract.model_dump_json(),
                    _now().isoformat(),
                ),
            )
            existing = connection.execute(
                """
                SELECT contract_json FROM phase3_completion_contracts
                WHERE contract_id = ? AND contract_version = ?
                """,
                (contract.contract_id, contract.contract_version),
            ).fetchone()
            if (
                existing is None
                or CompletionContract.model_validate_json(existing[0]) != contract
            ):
                raise ValueError("CompletionContract identity conflict")

    def create_task(
        self,
        contract: TaskContract,
        *,
        task_id: str,
        attempt_id: str,
        task_version: int = 1,
        attempt_version: int = 1,
    ) -> None:
        raise RuntimeError("Task must be submitted through TaskRuntime")

    def begin_execution(self, *, task_id: str, attempt_id: str) -> None:
        raise RuntimeError("Execution lifecycle must be advanced by TaskRuntime")

    def expire_deadline(
        self,
        *,
        task_id: str,
        attempt_id: str,
        reason: str,
    ) -> None:
        raise RuntimeError("Deadline lifecycle must be advanced by TaskRuntime")

    def evaluate_execution_result(
        self, execution_id: str
    ) -> GovernanceEvaluationResult:
        """Evaluate one persisted ExecutionResult without trusting its status."""

        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT
                    result.result_json,
                    result.result_hash,
                    result.created_at AS result_created_at,
                    execution.task_id,
                    execution.attempt_id,
                    task.contract_json,
                    task.version AS task_version,
                    task.state AS task_state,
                    task.current_attempt_id,
                    attempt.version AS attempt_version,
                    attempt.state AS attempt_state
                FROM phase4_execution_results AS result
                JOIN executions AS execution
                    ON execution.execution_id = result.execution_id
                JOIN phase3_tasks AS task ON task.task_id = execution.task_id
                JOIN phase3_attempts AS attempt
                    ON attempt.attempt_id = execution.attempt_id
                WHERE result.execution_id = ?
                """,
                (execution_id,),
            ).fetchone()
            if row is None:
                raise KeyError(execution_id)
            if row["task_state"] != TaskState.RUNNING.value:
                raise RuntimeError("Task is not running")
            if (
                row["current_attempt_id"] != row["attempt_id"]
                or row["attempt_state"] != AttemptState.ACTIVE.value
            ):
                raise RuntimeError("Execution attempt is not active and current")

            result = ExecutionResult.model_validate_json(row["result_json"])
            contract = effective_contract(
                self.store.authority_store,
                str(row["task_id"]),
                TaskContract.model_validate_json(row["contract_json"]),
            )
            completion_row = connection.execute(
                """
                SELECT contract_json FROM phase3_completion_contracts
                WHERE contract_id = ? AND contract_version = ?
                """,
                (
                    contract.completion_contract_ref.contract_id,
                    contract.completion_contract_ref.contract_version,
                ),
            ).fetchone()
            if completion_row is None:
                raise RuntimeError(
                    "Referenced CompletionContract is not registered"
                )
            completion_contract = CompletionContract.model_validate_json(
                completion_row[0]
            )
            if completion_contract.task_type != contract.task_type:
                raise RuntimeError("CompletionContract task_type mismatch")
            submitted_payload = dict(result.submitted_result or {})
            nested_outcome = submitted_payload.get("outcome")
            outcome = (
                dict(nested_outcome)
                if isinstance(nested_outcome, Mapping)
                else submitted_payload
            )
            direct_response_validated = not (
                contract.execution_type == ExecutionType.DIRECT_RESPONSE
            ) or result.task_outcome_validated
            if not direct_response_validated:
                outcome = {}
            submitted_evidence_refs = submitted_payload.get(
                "evidence_refs",
                (result.result_receipt or {}).get("evidence_refs", ()),
            )
            submitted_receipt_refs = submitted_payload.get(
                "receipt_refs",
                (result.result_receipt or {}).get("receipt_refs", ()),
            )
            submitted = SubmittedResult(
                submitted_result_id="submitted-result:"
                + sha256_digest(
                    {
                        "execution_id": execution_id,
                        "result_hash": row["result_hash"],
                    }
                ),
                task_id=row["task_id"],
                attempt_id=row["attempt_id"],
                execution_id=execution_id,
                outcome=outcome,
                evidence_refs=tuple(
                    str(value) for value in submitted_evidence_refs
                ),
                receipt_refs=tuple(
                    str(value) for value in submitted_receipt_refs
                ),
            )
            receipt = dict(result.result_receipt or {})
            receipt_id = receipt.get("receipt_id")
            receipt_hashes = self._receipt_snapshot(
                connection, row["task_id"], row["attempt_id"]
            )
            if receipt_id and str(receipt_id) not in receipt_hashes:
                receipt_hashes[str(receipt_id)] = sha256_digest(receipt)
            receipt_refs = tuple(sorted(receipt_hashes))
            interaction_refs, interaction_states, pending_interaction_kinds = (
                self._interaction_snapshot(connection, row["task_id"])
            )
            external_operations = self._external_operations(
                connection, row["task_id"]
            )
            approval_required, approval_matched, _ = (
                self._approval_binding_state(
                    connection,
                    contract,
                    external_operations,
                )
            )
            approval_refs = self._approval_snapshot(
                connection, row["task_id"]
            )
            denied_effect_refs = self._denied_approval_effects(
                connection,
                row["task_id"],
                external_operations,
            )
            failed_effect_refs = {
                operation.effect_identity: operation.effect_request_hash
                for operation in external_operations
                if operation.status == ExternalOperationStatus.FAILED
            }
            governed_failure_refs = tuple(
                "failure:" + sha256_digest(failure)
                for failure in result.metadata.get("governed_tool_failures", ())
                if isinstance(failure, Mapping)
            )
            canonical_effects = self._canonical_effects(
                connection, row["task_id"]
            )
            business_observations = self._business_observations(
                connection, row["task_id"]
            )
            execution_refs = self._execution_snapshot(
                connection, row["task_id"]
            )
            completion_refs = self._completion_snapshot(
                connection,
                row["task_id"],
                excluded_submitted_result_id=submitted.submitted_result_id,
            )
            fact_watermark = self._fact_watermark(
                connection, row["task_id"]
            )
            supporting_facts = tuple(
                {
                    **json.loads(fact_row["fact_json"]),
                    "fact_id": str(fact_row["fact_id"]),
                    "fact_type": str(fact_row["fact_type"]),
                }
                for fact_row in connection.execute(
                    """
                    SELECT fact_id, fact_type, fact_json
                    FROM phase3_reliability_facts
                    WHERE task_id = ? AND sequence <= ?
                    ORDER BY sequence
                    """,
                    (row["task_id"], fact_watermark),
                ).fetchall()
            )
            attempt_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM phase3_attempts WHERE task_id = ?",
                    (row["task_id"],),
                ).fetchone()[0]
            )
            execution_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM executions WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()[0]
            )
            feedback_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM phase4_run_requests
                    WHERE attempt_id = ? AND reason = 'continue_with_feedback'
                    """,
                    (row["attempt_id"],),
                ).fetchone()[0]
            )
            reconcile_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM phase3_reconciliations WHERE attempt_id = ?",
                    (row["attempt_id"],),
                ).fetchone()[0]
            )
            collector_version = "astra.evidence_collector@1"
            collection_trigger_id = submitted.submitted_result_id
            authoritative_versions = {
                "task": {
                    "task_id": row["task_id"],
                    "version": row["task_version"],
                    "state": row["task_state"],
                },
                "contract": contract.ref.model_dump(mode="json"),
                "attempt": {
                    "attempt_id": row["attempt_id"],
                    "version": row["attempt_version"],
                    "state": row["attempt_state"],
                },
                "executions": execution_refs,
                "interactions": {
                    interaction_id: {
                        "version": version,
                        "state": interaction_states[interaction_id],
                    }
                    for interaction_id, version in interaction_refs.items()
                },
                "approvals": approval_refs,
                "external_operations": {
                    operation.operation_id: {
                        "version": operation.version,
                        "status": operation.status.value,
                        "content_hash": sha256_digest(operation),
                    }
                    for operation in external_operations
                },
                "canonical_effects": {
                    effect.effect_identity: effect.effect_request_hash
                    for effect in canonical_effects
                },
                "receipts": receipt_hashes,
                "observations": {
                    observation.observation_id: {
                        "version": observation.observation_version,
                        "content_hash": observation.content_hash,
                    }
                    for observation in business_observations
                },
                "fact_watermark": fact_watermark,
                "completion_refs": completion_refs,
                "task_rules": [
                    {"rule_id": rule_id, "rule_version": rule_version}
                    for rule_id, rule_version in self.task_rule_registry.registered_refs
                ],
            }
            authoritative_versions_hash, evidence_snapshot_id = (
                build_evidence_snapshot_identity(
                    task_id=row["task_id"],
                    collection_trigger_id=collection_trigger_id,
                    authoritative_versions=authoritative_versions,
                    collector_version=collector_version,
                )
            )
            snapshot = EvidenceSnapshot.materialize(
                evidence_snapshot_id=evidence_snapshot_id,
                task_id=row["task_id"],
                attempt_id=row["attempt_id"],
                collection_trigger_id=collection_trigger_id,
                authoritative_versions_hash=authoritative_versions_hash,
                authoritative_versions=authoritative_versions,
                task_version=row["task_version"],
                attempt_version=row["attempt_version"],
                task_contract_ref=contract.ref,
                execution_refs=execution_refs,
                receipt_refs=receipt_refs,
                receipt_hashes=receipt_hashes,
                interaction_refs=interaction_refs,
                interaction_states=interaction_states,
                external_operations=external_operations,
                canonical_effects=canonical_effects,
                effect_refs={
                    operation.effect_identity: operation.effect_request_hash
                    for operation in external_operations
                },
                approval_refs=approval_refs,
                business_observations=business_observations,
                business_observation_refs=tuple(
                    observation.observation_id
                    for observation in business_observations
                ),
                completion_refs=completion_refs,
                pending_interaction_kinds=tuple(
                    sorted(pending_interaction_kinds)
                ),
                approval_required=approval_required,
                approval_matched=approval_matched,
                denied_effect_refs=denied_effect_refs,
                failed_effect_refs=failed_effect_refs,
                submitted_result_present=(
                    result.submitted_result is not None
                    and direct_response_validated
                ),
                runtime_counters={
                    "attempt_count": attempt_count,
                    "execution_count": execution_count,
                    "feedback_count": feedback_count,
                    "reconcile_count": reconcile_count,
                },
                fact_watermark=fact_watermark,
                collector_version=collector_version,
                created_at=datetime.fromisoformat(
                    str(row["result_created_at"]).replace("Z", "+00:00")
                ),
            )
            snapshot = self._record_evidence_snapshot(connection, snapshot)
        evaluations = tuple(
            self.evaluator_registry.evaluate(requirement, submitted, snapshot)
            for requirement in completion_contract.requirements
        )
        completion = aggregate_completion(
            completion_contract,
            submitted,
            snapshot,
            evaluations,
        )
        self.record_requirement_evaluations(row["task_id"], evaluations)
        rule_evaluations = self.task_rule_registry.evaluate_all(
            TaskRuleContext(
                decision_point=DecisionPoint.COMPLETION_VALIDATED,
                evidence_snapshot=snapshot,
                completion_validation=completion,
                supporting_facts=supporting_facts,
            )
        )
        self.record_rule_evaluations(row["task_id"], rule_evaluations)
        findings = tuple(
            finding
            for evaluation in rule_evaluations
            for finding in evaluation.findings
        )
        operation_status = next(
            (
                operation.status
                for operation in snapshot.external_operations
                if operation.status == ExternalOperationStatus.INDETERMINATE
            ),
            None,
        )
        user_input_pending = "user_input" in snapshot.pending_interaction_kinds
        necessary_input_missing = bool(
            user_input_pending
            or (
                not snapshot.submitted_result_present
                and not snapshot.denied_effect_refs
                and not snapshot.failed_effect_refs
                and not governed_failure_refs
                and not snapshot.receipt_refs
                and not snapshot.external_operations
            )
        )
        action, reason_code = choose_policy_action(
            input_complete=not necessary_input_missing,
            approval_required=snapshot.approval_required,
            approval_matched=snapshot.approval_matched,
            approval_denied=bool(snapshot.denied_effect_refs),
            governed_effect_failed=bool(
                snapshot.failed_effect_refs or governed_failure_refs
            ) and not user_input_pending,
            external_operation_status=operation_status,
            completion_status=completion.status,
        )
        interaction_spec: InteractionSpec | None = None
        feedback: PolicyFeedback | None = None
        if action == PolicyAction.REQUEST_INPUT:
            interaction_spec = InteractionSpec(
                kind="user_input",
                reason_code=reason_code,
                required_information=("submitted_result",),
                evidence_refs=tuple(submitted.evidence_refs),
                active_constraints=tuple(
                    constraint.constraint_id for constraint in contract.constraints
                ),
            )
        elif action == PolicyAction.REQUEST_APPROVAL:
            interaction_spec = self._approval_interaction_spec(
                contract, external_operations, reason_code=reason_code
            )
            if interaction_spec is None:
                action = PolicyAction.CONTINUE_WITH_FEEDBACK
                reason_code = "exact_effect_request_required"

        if action == PolicyAction.CONTINUE_WITH_FEEDBACK:
            feedback = PolicyFeedback(
                facts=tuple(
                    dict.fromkeys(
                        (
                            evaluation.message
                            for evaluation in evaluations
                            if evaluation.message
                        )
                    )
                )
                + tuple(
                    dict.fromkeys(
                        finding.message
                        for finding in findings
                    )
                ),
                unmet_requirements=tuple(
                    dict.fromkeys(
                        (*completion.unmet_requirement_ids, *completion.unknown_requirement_ids)
                    )
                ),
                active_constraints=tuple(
                    constraint.constraint_id for constraint in contract.constraints
                ),
            )

        action, reason_code = self._apply_frozen_bounds(
            action=action,
            reason_code=reason_code,
            contract=contract,
            attempt_count=snapshot.runtime_counters["attempt_count"],
            execution_count=snapshot.runtime_counters["execution_count"],
            feedback_count=snapshot.runtime_counters["feedback_count"],
            reconcile_count=snapshot.runtime_counters["reconcile_count"],
            now=_now(),
        )
        if action != PolicyAction.CONTINUE_WITH_FEEDBACK:
            feedback = None
        if action not in {PolicyAction.REQUEST_INPUT, PolicyAction.REQUEST_APPROVAL}:
            interaction_spec = None
        context = DecisionContext.materialize(
            decision_context_id="context:" + snapshot.evidence_snapshot_id,
            decision_point=DecisionPoint.COMPLETION_VALIDATED,
            trigger_id=completion.completion_validation_id,
            task_id=row["task_id"],
            task_version=row["task_version"],
            task_contract_ref=contract.ref,
            attempt_id=row["attempt_id"],
            attempt_version=row["attempt_version"],
            evidence_snapshot_id=snapshot.evidence_snapshot_id,
            fact_watermark=snapshot.fact_watermark,
            completion_validation_id=completion.completion_validation_id,
            interaction_snapshot_version=max(interaction_refs.values(), default=0),
        )
        decision = make_policy_decision(
            context,
            action=action,
            reason_code=reason_code,
            finding_refs=tuple(finding.finding_id for finding in findings),
            evaluation_refs=tuple(
                evaluation.evaluation_id for evaluation in evaluations
            )
            + tuple(
                evaluation.rule_evaluation_id
                for evaluation in rule_evaluations
            ),
            fact_refs=tuple(
                str(fact["fact_id"])
                for fact in supporting_facts
                if fact.get("fact_id") is not None
            ),
            feedback=feedback,
            interaction_spec=interaction_spec,
        )
        self.record_completion_validation(row["task_id"], completion)
        self.record_policy_decision(decision)
        return GovernanceEvaluationResult(
            execution_id=execution_id,
            evidence_snapshot=snapshot,
            requirement_evaluations=evaluations,
            rule_evaluations=rule_evaluations,
            completion_validation=completion,
            policy_decision=decision,
        )

    def evaluate(self, execution_id: str) -> GovernanceEvaluationResult:
        """Short callable surface used by the Phase 4 worker."""

        return self.evaluate_execution_result(execution_id)

    @staticmethod
    def _approval_interaction_spec(
        contract: TaskContract,
        operations: tuple[ExternalOperation, ...],
        *,
        reason_code: str,
    ) -> InteractionSpec | None:
        for operation in operations:
            candidates = tuple(
                intent
                for intent in contract.authorized_effects
                if intent.approval_requirement_ref is not None
                and intent.authority_domain == operation.authority_domain
                and intent.effect_type == operation.effect_type
                and intent.effect_type_version == operation.effect_type_version
                and intent.subject_ref == operation.subject_ref
            )
            if len(candidates) != 1:
                continue
            requirement_ref = candidates[0].approval_requirement_ref
            assert requirement_ref is not None
            return InteractionSpec(
                kind="approval",
                reason_code=reason_code,
                approval_requirement_ref=requirement_ref,
                effect_identity=operation.effect_identity,
                effect_request_hash=operation.effect_request_hash,
                permission_scope="execute_effect",
                evidence_refs=(operation.operation_id,),
                active_constraints=tuple(
                    constraint.constraint_id for constraint in contract.constraints
                ),
            )
        return None

    @staticmethod
    def _apply_frozen_bounds(
        *,
        action: PolicyAction,
        reason_code: str,
        contract: TaskContract,
        attempt_count: int,
        execution_count: int,
        feedback_count: int,
        reconcile_count: int,
        now: datetime,
    ) -> tuple[PolicyAction, str]:
        limits = contract.limits
        if now >= _instant(limits.task_deadline):
            return PolicyAction.FAIL, "task_deadline_exceeded"

        current_attempt_exhausted = bool(
            limits.attempt_deadline is not None
            and now >= _instant(limits.attempt_deadline)
        )
        if action == PolicyAction.CONTINUE_WITH_FEEDBACK:
            current_attempt_exhausted = current_attempt_exhausted or (
                execution_count >= limits.max_executions_per_attempt
                or feedback_count >= limits.max_feedback_cycles
            )
        elif action == PolicyAction.RECONCILE:
            current_attempt_exhausted = current_attempt_exhausted or (
                reconcile_count >= limits.max_reconcile_cycles
            )

        if current_attempt_exhausted:
            if attempt_count < limits.max_attempts:
                return PolicyAction.START_NEW_ATTEMPT, "attempt_budget_exhausted"
            return PolicyAction.FAIL, "attempt_budget_exhausted"

        if (
            action == PolicyAction.START_NEW_ATTEMPT
            and attempt_count >= limits.max_attempts
        ):
            return PolicyAction.FAIL, "attempt_budget_exhausted"
        return action, reason_code

    def apply(self, decision_id: str) -> GovernanceApplicationResult:
        """Reject lifecycle mutation outside the TaskRuntime authority."""

        raise RuntimeError("PolicyDecision must be applied by TaskRuntime")

    @staticmethod
    def _fact_watermark(
        connection: sqlite3.Connection, task_id: str
    ) -> int:
        row = connection.execute(
            """
            SELECT COALESCE(MAX(sequence), 0)
            FROM phase3_reliability_facts WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _receipt_snapshot(
        connection: sqlite3.Connection, task_id: str, attempt_id: str
    ) -> dict[str, str]:
        receipts: dict[str, str] = {}
        execution_rows = connection.execute(
            """
            SELECT * FROM execution_receipts
            WHERE task_id = ? AND attempt_id = ?
            ORDER BY receipt_id
            """,
            (task_id, attempt_id),
        ).fetchall()
        result_rows = connection.execute(
            """
            SELECT receipt.*
            FROM result_receipts AS receipt
            JOIN executions AS execution
              ON execution.execution_id = receipt.execution_id
            WHERE receipt.task_id = ? AND execution.attempt_id = ?
            ORDER BY receipt.receipt_id
            """,
            (task_id, attempt_id),
        ).fetchall()
        for row in (*execution_rows, *result_rows):
            payload = dict(row)
            receipts[str(payload["receipt_id"])] = sha256_digest(payload)
        return receipts

    @staticmethod
    def _execution_snapshot(
        connection: sqlite3.Connection, task_id: str
    ) -> dict[str, str]:
        rows = connection.execute(
            """
            SELECT execution.*, result.result_hash, result.created_at AS result_created_at
            FROM executions AS execution
            LEFT JOIN phase4_execution_results AS result
              ON result.execution_id = execution.execution_id
            WHERE execution.task_id = ?
            ORDER BY execution.execution_id
            """,
            (task_id,),
        ).fetchall()
        return {
            str(row["execution_id"]): sha256_digest(dict(row)) for row in rows
        }

    @staticmethod
    def _external_operations(
        connection: sqlite3.Connection, task_id: str
    ) -> tuple[ExternalOperation, ...]:
        rows = connection.execute(
            """
            SELECT operation_json FROM phase3_external_operations
            WHERE task_id = ? ORDER BY operation_id
            """,
            (task_id,),
        ).fetchall()
        return tuple(
            ExternalOperation.model_validate_json(row[0]) for row in rows
        )

    @staticmethod
    def _canonical_effects(
        connection: sqlite3.Connection, task_id: str
    ) -> tuple[CanonicalEffectRequest, ...]:
        rows = connection.execute(
            """
            SELECT request_json FROM phase3_canonical_effect_requests
            WHERE task_id = ? ORDER BY effect_identity
            """,
            (task_id,),
        ).fetchall()
        return tuple(
            CanonicalEffectRequest.model_validate_json(row[0]) for row in rows
        )

    @staticmethod
    def _business_observations(
        connection: sqlite3.Connection, task_id: str
    ) -> tuple[BusinessObservation, ...]:
        rows = connection.execute(
            """
            SELECT observation_json
            FROM phase3_business_observations AS observation
            WHERE task_id = ?
              AND observation_version = (
                  SELECT MAX(latest.observation_version)
                  FROM phase3_business_observations AS latest
                  WHERE latest.observation_id = observation.observation_id
              )
            ORDER BY observation_id
            """,
            (task_id,),
        ).fetchall()
        return tuple(
            BusinessObservation.model_validate_json(row[0]) for row in rows
        )

    @staticmethod
    def _interaction_snapshot(
        connection: sqlite3.Connection, task_id: str
    ) -> tuple[dict[str, int], dict[str, str], frozenset[str]]:
        rows = connection.execute(
            """
            SELECT interaction_id, kind, status, version
            FROM interactions
            WHERE task_id = ? ORDER BY interaction_id
            """,
            (task_id,),
        ).fetchall()
        return (
            {str(row["interaction_id"]): int(row["version"]) for row in rows},
            {str(row["interaction_id"]): str(row["status"]) for row in rows},
            frozenset(
                str(row["kind"]) for row in rows if row["status"] == "pending"
            ),
        )

    @staticmethod
    def _approval_snapshot(
        connection: sqlite3.Connection, task_id: str
    ) -> dict[str, str]:
        refs: dict[str, str] = {}
        requests = connection.execute(
            """
            SELECT * FROM phase3_approval_requests
            WHERE task_id = ? ORDER BY approval_request_id
            """,
            (task_id,),
        ).fetchall()
        for row in requests:
            refs[str(row["approval_request_id"])] = sha256_digest(dict(row))
        resolutions = connection.execute(
            """
            SELECT * FROM phase3_approval_resolutions
            WHERE task_id = ? ORDER BY approval_resolution_id
            """,
            (task_id,),
        ).fetchall()
        for row in resolutions:
            refs[str(row["approval_resolution_id"])] = sha256_digest(dict(row))
        return refs

    @staticmethod
    def _denied_approval_effects(
        connection: sqlite3.Connection,
        task_id: str,
        external_operations: tuple[ExternalOperation, ...],
    ) -> dict[str, str]:
        rows = connection.execute(
            """
            SELECT resolution.effect_identity,
                   resolution.effect_request_hash,
                   resolution.resolution_json,
                   request.request_json
            FROM phase3_approval_resolutions AS resolution
            JOIN phase3_approval_requests AS request
              ON request.approval_request_id = resolution.approval_request_id
             AND request.interaction_id = resolution.interaction_id
             AND request.task_id = resolution.task_id
            WHERE resolution.task_id = ?
              AND resolution.decision = 'denied'
              AND request.status = 'resolved'
            ORDER BY resolution.approval_resolution_id
            """,
            (task_id,),
        ).fetchall()
        executed_effects = {
            operation.effect_identity: operation.effect_request_hash
            for operation in external_operations
        }
        denied: dict[str, str] = {}
        for row in rows:
            resolution = ApprovalResolution.model_validate_json(
                row["resolution_json"]
            )
            request = ApprovalRequest.model_validate_json(row["request_json"])
            if (
                resolution.decision == ApprovalDecision.DENIED
                and resolution.effect_identity == row["effect_identity"]
                and resolution.effect_request_hash == row["effect_request_hash"]
                and request.approval_request_id
                == resolution.approval_request_id
                and request.interaction_id == resolution.interaction_id
                and request.effect_identity == resolution.effect_identity
                and request.effect_request_hash
                == resolution.effect_request_hash
                and request.task_contract_ref
                == resolution.task_contract_ref
                and request.approval_requirement_ref
                == resolution.approval_requirement_ref
                and executed_effects.get(resolution.effect_identity) is None
            ):
                denied[resolution.effect_identity] = resolution.effect_request_hash
        return denied

    @staticmethod
    def _completion_snapshot(
        connection: sqlite3.Connection,
        task_id: str,
        *,
        excluded_submitted_result_id: str,
    ) -> dict[str, str]:
        refs: dict[str, str] = {}
        rows = connection.execute(
            """
            SELECT completion_validation_id, validation_json
            FROM phase3_completion_validations
            WHERE task_id = ? ORDER BY completion_validation_id
            """,
            (task_id,),
        ).fetchall()
        for row in rows:
            validation = CompletionValidationResult.model_validate_json(
                row["validation_json"]
            )
            if validation.submitted_result_id == excluded_submitted_result_id:
                continue
            refs[str(row["completion_validation_id"])] = sha256_digest(
                validation
            )
        return refs

    @staticmethod
    def _approval_binding_state(
        connection: sqlite3.Connection,
        contract: TaskContract,
        operations: tuple[ExternalOperation, ...],
    ) -> tuple[bool, bool, dict[str, str]]:
        approval_required = any(
            intent.approval_requirement_ref is not None
            for intent in contract.authorized_effects
        )
        if not approval_required:
            return False, True, {}
        if not operations:
            return True, False, {}

        approval_refs: dict[str, str] = {}
        for operation in operations:
            if operation.task_contract_ref != contract.ref:
                return True, False, approval_refs
            candidates = tuple(
                intent
                for intent in contract.authorized_effects
                if intent.approval_requirement_ref is not None
                and intent.authority_domain == operation.authority_domain
                and intent.effect_type == operation.effect_type
                and intent.effect_type_version == operation.effect_type_version
                and intent.subject_ref == operation.subject_ref
            )
            if len(candidates) != 1 or operation.approval_ref is None:
                return True, False, approval_refs
            intent = candidates[0]
            row = connection.execute(
                """
                SELECT resolution_json, usage_count, revoked
                FROM phase3_approval_resolutions
                WHERE approval_resolution_id = ?
                """,
                (operation.approval_ref,),
            ).fetchone()
            if row is None:
                return True, False, approval_refs
            stored = ApprovalResolution.model_validate_json(row["resolution_json"])
            resolution = stored.model_copy(
                update={
                    "usage_count": int(row["usage_count"]),
                    "revoked": bool(row["revoked"]),
                }
            )
            requirement_ref = intent.approval_requirement_ref
            assert requirement_ref is not None
            requirement = contract.approval_requirement(requirement_ref)
            request_row = connection.execute(
                """
                SELECT request_json, status FROM phase3_approval_requests
                WHERE approval_request_id = ? AND interaction_id = ?
                """,
                (resolution.approval_request_id, resolution.interaction_id),
            ).fetchone()
            interaction_row = connection.execute(
                """
                SELECT status FROM interactions
                WHERE interaction_id = ? AND task_id = ?
                """,
                (resolution.interaction_id, operation.task_id),
            ).fetchone()
            if request_row is None or interaction_row is None:
                return True, False, approval_refs
            request = ApprovalRequest.model_validate_json(request_row["request_json"])
            current = _now()
            if (
                resolution.decision != ApprovalDecision.APPROVED
                or resolution.revoked
                or current < resolution.valid_from
                or (
                    resolution.expires_at is not None
                    and current >= resolution.expires_at
                )
                or request_row["status"] != "resolved"
                or interaction_row["status"] != "resolved"
                or request.task_contract_ref != contract.ref
                or request.approval_requirement_ref.ref != requirement_ref
                or request.effect_identity != operation.effect_identity
                or request.effect_request_hash != operation.effect_request_hash
                or request.permission_scope != "execute_effect"
                or request.approver_policy_ref != requirement.approver_policy_ref
                or resolution.task_contract_ref != contract.ref
                or resolution.effect_identity != operation.effect_identity
                or resolution.effect_request_hash != operation.effect_request_hash
                or resolution.approval_requirement_ref.ref != requirement_ref
                or resolution.approver_policy_ref
                != requirement.approver_policy_ref
                or resolution.permission_scope != "execute_effect"
                or resolution.usage_semantics != requirement.usage_semantics
            ):
                return True, False, approval_refs
            approval_refs[resolution.approval_resolution_id] = sha256_digest(
                {
                    "resolution": resolution,
                    "request_status": request_row["status"],
                    "interaction_status": interaction_row["status"],
                }
            )
        return True, True, approval_refs

    def record_approval_resolution(
        self, task_id: str, resolution: ApprovalResolution
    ) -> None:
        """Reject Approval lifecycle mutation outside TaskRuntime.resolve."""

        raise RuntimeError("ApprovalResolution must be recorded by TaskRuntime")

    def _record_approval_resolution_from_runtime(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        resolution: ApprovalResolution,
    ) -> None:
        """Persist an exact Resolution inside the Runtime resolve transaction."""

        if connection is not self.store.connection or not connection.in_transaction:
            raise ValueError(
                "ApprovalResolution must use the authoritative AstraStore transaction"
            )

        connection.execute(
            """
            INSERT INTO phase3_approval_resolutions(
                approval_resolution_id, approval_request_id,
                interaction_id, task_id, effect_identity,
                effect_request_hash, decision, usage_semantics, usage_count,
                revoked, resolution_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(approval_resolution_id) DO NOTHING
            """,
            (
                resolution.approval_resolution_id,
                resolution.approval_request_id,
                resolution.interaction_id,
                task_id,
                resolution.effect_identity,
                resolution.effect_request_hash,
                resolution.decision.value,
                resolution.usage_semantics.value,
                resolution.usage_count,
                int(resolution.revoked),
                resolution.model_dump_json(),
                _now().isoformat(),
            ),
        )
        existing = connection.execute(
            """
            SELECT task_id, resolution_json
            FROM phase3_approval_resolutions
            WHERE approval_resolution_id = ?
            """,
            (resolution.approval_resolution_id,),
        ).fetchone()
        if (
            existing is None
            or existing["task_id"] != task_id
            or ApprovalResolution.model_validate_json(existing["resolution_json"])
            != resolution
        ):
            raise ValueError("ApprovalResolution identity conflict")

    def prepare_external_operation(
        self,
        effect: CanonicalEffectRequest,
        *,
        task_id: str,
        attempt_id: str,
        execution_id: str,
        approval_resolution_id: str | None = None,
        operation_id: str | None = None,
        now: datetime | None = None,
    ) -> tuple[ExternalOperation, bool]:
        """Atomically verify/consume approval and prepare one effect operation."""

        current_time = now or _now()
        with self.store.transaction() as connection:
            task = connection.execute(
                """
                SELECT task.contract_json, task.state AS task_state,
                       task.current_attempt_id,
                       attempt.state AS attempt_state,
                       execution.status AS execution_status,
                       execution.ended_at,
                       request.state AS run_request_state
                FROM phase3_tasks AS task
                JOIN phase3_attempts AS attempt
                  ON attempt.attempt_id = ? AND attempt.task_id = task.task_id
                JOIN executions AS execution
                  ON execution.execution_id = ?
                 AND execution.task_id = task.task_id
                 AND execution.attempt_id = attempt.attempt_id
                LEFT JOIN phase4_run_requests AS request
                  ON request.run_request_id = execution.run_request_id
                WHERE task.task_id = ?
                """,
                (attempt_id, execution_id, task_id),
            ).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["task_state"] != TaskState.RUNNING.value:
                raise PermissionError("task_not_effectively_running")
            if (
                task["current_attempt_id"] != attempt_id
                or task["attempt_state"] != AttemptState.ACTIVE.value
            ):
                raise PermissionError("attempt_not_active_or_current")
            if (
                task["execution_status"] != "running"
                or task["ended_at"] is not None
                or task["run_request_state"] != "claimed"
            ):
                raise PermissionError("execution_not_active")
            contract = effective_contract(
                self.store.authority_store,
                task_id,
                TaskContract.model_validate_json(task["contract_json"]),
            )
            if effect.task_contract_ref != contract.ref:
                raise PermissionError("effect_stale_contract")
            connection.execute(
                """
                INSERT INTO phase3_canonical_effect_requests(
                    effect_identity, effect_request_hash, task_id, attempt_id,
                    execution_id, authority_domain, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(effect_identity) DO NOTHING
                """,
                (
                    effect.effect_identity,
                    effect.effect_request_hash,
                    task_id,
                    attempt_id,
                    execution_id,
                    effect.authority_domain,
                    effect.model_dump_json(),
                    current_time.isoformat(),
                ),
            )
            canonical_row = connection.execute(
                """
                SELECT effect_request_hash, request_json
                FROM phase3_canonical_effect_requests
                WHERE effect_identity = ?
                """,
                (effect.effect_identity,),
            ).fetchone()
            if (
                canonical_row is None
                or canonical_row["effect_request_hash"]
                != effect.effect_request_hash
                or CanonicalEffectRequest.model_validate_json(
                    canonical_row["request_json"]
                )
                != effect
            ):
                raise RuntimeError("canonical_effect_identity_conflict")
            existing_row = connection.execute(
                """
                SELECT operation_json FROM phase3_external_operations
                WHERE authority_domain = ? AND effect_identity = ?
                """,
                (effect.authority_domain, effect.effect_identity),
            ).fetchone()
            existing = (
                ExternalOperation.model_validate_json(existing_row[0])
                if existing_row is not None
                else None
            )

            resolution = None
            if approval_resolution_id is not None:
                approval_row = connection.execute(
                    """
                    SELECT * FROM phase3_approval_resolutions
                    WHERE approval_resolution_id = ? AND task_id = ?
                    """,
                    (approval_resolution_id, task_id),
                ).fetchone()
                if approval_row is None:
                    raise PermissionError("approval_required")
                stored = ApprovalResolution.model_validate_json(
                    approval_row["resolution_json"]
                )
                resolution = stored.model_copy(
                    update={
                        "usage_count": approval_row["usage_count"],
                        "revoked": bool(approval_row["revoked"]),
                    }
                )
                request_row = connection.execute(
                    """
                    SELECT request_json, status FROM phase3_approval_requests
                    WHERE approval_request_id = ? AND interaction_id = ?
                    """,
                    (
                        resolution.approval_request_id,
                        resolution.interaction_id,
                    ),
                ).fetchone()
                interaction_row = connection.execute(
                    "SELECT status FROM interactions WHERE interaction_id = ?",
                    (resolution.interaction_id,),
                ).fetchone()
                if (
                    request_row is None
                    or request_row["status"] != "resolved"
                    or interaction_row is None
                    or interaction_row["status"] != "resolved"
                ):
                    raise PermissionError("approval_required")
                request = ApprovalRequest.model_validate_json(
                    request_row["request_json"]
                )
                if (
                    request.task_contract_ref != contract.ref
                    or request.effect_identity != effect.effect_identity
                    or request.effect_request_hash != effect.effect_request_hash
                    or request.permission_scope != resolution.permission_scope
                ):
                    raise PermissionError("approval_effect_mismatch")
            binding = verify_approval_binding(
                contract,
                effect,
                resolution,
                now=current_time,
                existing_operation_effect_identity=(
                    existing.effect_identity if existing is not None else None
                ),
            )
            if not binding.matched:
                raise PermissionError(binding.code.value)
            if existing is not None:
                if existing.effect_request_hash != effect.effect_request_hash:
                    raise RuntimeError("effect_identity_request_hash_conflict")
                return existing, False

            intent = contract.effect_intent(effect.effect_intent_ref)
            reserved_for_intent = 0
            for reserved_row in connection.execute(
                """
                SELECT request.request_json
                FROM phase3_external_operations AS operation
                JOIN phase3_canonical_effect_requests AS request
                  ON request.effect_identity = operation.effect_identity
                WHERE operation.task_id = ? AND operation.status != 'failed'
                """,
                (task_id,),
            ).fetchall():
                reserved_effect = CanonicalEffectRequest.model_validate_json(
                    reserved_row["request_json"]
                )
                if reserved_effect.effect_intent_ref == effect.effect_intent_ref:
                    reserved_for_intent += 1
            if reserved_for_intent >= intent.max_confirmed_occurrences:
                raise PermissionError("effect_occurrence_limit_exceeded")

            operation = ExternalOperation.from_effect(
                effect,
                operation_id=operation_id
                or "operation:" + sha256_digest(effect.effect_identity),
                task_id=task_id,
                attempt_id=attempt_id,
                execution_id=execution_id,
                approval_ref=(
                    resolution.approval_resolution_id if resolution else None
                ),
                created_at=current_time,
            )
            connection.execute(
                """
                INSERT INTO phase3_external_operations(
                    operation_id, task_id, attempt_id, authority_domain,
                    effect_identity, effect_request_hash, status, version,
                    required_for_completion, operation_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'prepared', 1, 1, ?, ?)
                """,
                (
                    operation.operation_id,
                    task_id,
                    attempt_id,
                    operation.authority_domain,
                    operation.effect_identity,
                    operation.effect_request_hash,
                    operation.model_dump_json(),
                    current_time.isoformat(),
                ),
            )
            if resolution is not None:
                connection.execute(
                    """
                    UPDATE phase3_approval_resolutions
                    SET usage_count = usage_count + 1
                    WHERE approval_resolution_id = ?
                    """,
                    (resolution.approval_resolution_id,),
                )
            self._publish_operation_fact(
                connection,
                operation,
                fact_type="side_effect_requested",
            )
            return operation, True

    def transition_external_operation(
        self,
        operation_id: str,
        status: ExternalOperationStatus,
        *,
        external_operation_id: str | None = None,
        actor_task_id: str | None = None,
        actor_attempt_id: str | None = None,
        actor_execution_id: str | None = None,
    ) -> ExternalOperation:
        """Persist an authoritative operation status/version transition."""

        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT operation_json FROM phase3_external_operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            operation = ExternalOperation.model_validate_json(row[0])
            if status in {
                ExternalOperationStatus.IN_FLIGHT,
                ExternalOperationStatus.ACKNOWLEDGED,
                ExternalOperationStatus.CONFIRMED,
            }:
                task_id = actor_task_id or operation.task_id
                attempt_id = actor_attempt_id or operation.attempt_id
                execution_id = actor_execution_id or operation.execution_id
                authority = connection.execute(
                    """
                    SELECT task.state AS task_state,
                           task.current_attempt_id,
                           attempt.state AS attempt_state,
                           execution.status AS execution_status,
                           execution.ended_at,
                           request.state AS run_request_state
                    FROM phase3_tasks AS task
                    JOIN phase3_attempts AS attempt
                      ON attempt.attempt_id = ?
                     AND attempt.task_id = task.task_id
                    JOIN executions AS execution
                      ON execution.execution_id = ?
                     AND execution.task_id = task.task_id
                     AND execution.attempt_id = attempt.attempt_id
                    LEFT JOIN phase4_run_requests AS request
                      ON request.run_request_id = execution.run_request_id
                    WHERE task.task_id = ?
                    """,
                    (attempt_id, execution_id, task_id),
                ).fetchone()
                if authority is None:
                    raise PermissionError("authoritative_execution_not_found")
                if authority["task_state"] != TaskState.RUNNING.value:
                    raise PermissionError("task_not_effectively_running")
                if (
                    authority["current_attempt_id"] != attempt_id
                    or authority["attempt_state"] != AttemptState.ACTIVE.value
                ):
                    raise PermissionError("attempt_not_active_or_current")
                if (
                    authority["execution_status"] != "running"
                    or authority["ended_at"] is not None
                    or authority["run_request_state"] != "claimed"
                ):
                    raise PermissionError("execution_not_active")
                if status == ExternalOperationStatus.CONFIRMED:
                    canonical_row = connection.execute(
                        """
                        SELECT request_json
                        FROM phase3_canonical_effect_requests
                        WHERE effect_identity = ?
                        """,
                        (operation.effect_identity,),
                    ).fetchone()
                    task_row = connection.execute(
                        "SELECT contract_json FROM phase3_tasks WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()
                    if canonical_row is None or task_row is None:
                        raise RuntimeError("canonical_effect_request_missing")
                    canonical = CanonicalEffectRequest.model_validate_json(
                        canonical_row["request_json"]
                    )
                    contract = effective_contract(
                        self.store.authority_store,
                        task_id,
                        TaskContract.model_validate_json(task_row["contract_json"]),
                    )
                    intent = contract.effect_intent(canonical.effect_intent_ref)
                    confirmed_for_intent = 0
                    for confirmed_row in connection.execute(
                        """
                        SELECT request.request_json
                        FROM phase3_external_operations AS confirmed_operation
                        JOIN phase3_canonical_effect_requests AS request
                          ON request.effect_identity = confirmed_operation.effect_identity
                        WHERE confirmed_operation.task_id = ?
                          AND confirmed_operation.status = 'confirmed'
                          AND confirmed_operation.operation_id != ?
                        """,
                        (task_id, operation_id),
                    ).fetchall():
                        confirmed_effect = CanonicalEffectRequest.model_validate_json(
                            confirmed_row["request_json"]
                        )
                        if (
                            confirmed_effect.effect_intent_ref
                            == canonical.effect_intent_ref
                        ):
                            confirmed_for_intent += 1
                    if confirmed_for_intent >= intent.max_confirmed_occurrences:
                        raise PermissionError("effect_occurrence_limit_exceeded")
            if status == operation.status:
                return operation
            allowed_transitions = {
                ExternalOperationStatus.PREPARED: {
                    ExternalOperationStatus.IN_FLIGHT,
                    ExternalOperationStatus.ACKNOWLEDGED,
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.INDETERMINATE,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.IN_FLIGHT: {
                    ExternalOperationStatus.ACKNOWLEDGED,
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.INDETERMINATE,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.ACKNOWLEDGED: {
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.INDETERMINATE,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.INDETERMINATE: {
                    ExternalOperationStatus.CONFIRMED,
                    ExternalOperationStatus.FAILED,
                },
                ExternalOperationStatus.CONFIRMED: set(),
                ExternalOperationStatus.FAILED: set(),
            }
            if status not in allowed_transitions[operation.status]:
                raise ValueError(
                    f"Invalid ExternalOperation transition: "
                    f"{operation.status.value} -> {status.value}"
                )
            updates: dict[str, Any] = {
                "status": status,
                "version": operation.version + 1,
            }
            if external_operation_id is not None:
                updates["external_operation_id"] = external_operation_id
            if status == ExternalOperationStatus.ACKNOWLEDGED:
                updates["acknowledged_at"] = _now()
            if status == ExternalOperationStatus.CONFIRMED:
                updates["confirmed_at"] = _now()
            changed = operation.model_copy(update=updates)
            cursor = connection.execute(
                """
                UPDATE phase3_external_operations
                SET status = ?, version = ?, operation_json = ?
                WHERE operation_id = ? AND version = ?
                """,
                (
                    status.value,
                    changed.version,
                    changed.model_dump_json(),
                    operation_id,
                    operation.version,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("ExternalOperation CAS failed")
            fact_type = {
                ExternalOperationStatus.IN_FLIGHT: "external_operation_dispatched",
                ExternalOperationStatus.ACKNOWLEDGED: "external_operation_acknowledged",
                ExternalOperationStatus.CONFIRMED: "external_operation_confirmed",
                ExternalOperationStatus.INDETERMINATE: "external_operation_indeterminate",
                ExternalOperationStatus.FAILED: "external_operation_failed",
            }[status]
            self._publish_operation_fact(
                connection,
                changed,
                fact_type=fact_type,
            )
            return changed

    def record_business_observation(
        self,
        operation_id: str,
        state: Mapping[str, Any],
        *,
        external_object_id: str,
    ) -> BusinessObservation:
        """Persist or reuse one immutable authoritative business observation."""

        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT operation_json FROM phase3_external_operations
                WHERE operation_id = ?
                """,
                (operation_id,),
            ).fetchone()
            if row is None:
                raise KeyError(operation_id)
            operation = ExternalOperation.model_validate_json(row[0])
            if operation.external_operation_id not in {None, external_object_id}:
                raise ValueError("Observation external object identity mismatch")
            observation_id = "observation:" + sha256_digest(
                {
                    "operation_id": operation.operation_id,
                    "external_object_id": external_object_id,
                }
            )
            candidate = BusinessObservation.materialize(
                observation_id=observation_id,
                observation_version=1,
                task_id=operation.task_id,
                operation_id=operation.operation_id,
                authority_domain=operation.authority_domain,
                effect_identity=operation.effect_identity,
                effect_request_hash=operation.effect_request_hash,
                external_object_id=external_object_id,
                payload=dict(state),
            )
            existing = connection.execute(
                """
                SELECT observation_json
                FROM phase3_business_observations
                WHERE observation_id = ? AND content_hash = ?
                """,
                (observation_id, candidate.content_hash),
            ).fetchone()
            if existing is not None:
                return BusinessObservation.model_validate_json(
                    existing["observation_json"]
                )
            version_row = connection.execute(
                """
                SELECT COALESCE(MAX(observation_version), 0)
                FROM phase3_business_observations
                WHERE observation_id = ?
                """,
                (observation_id,),
            ).fetchone()
            version = int(version_row[0]) + 1
            observation = candidate.model_copy(
                update={"observation_version": version}
            )
            connection.execute(
                """
                INSERT INTO phase3_business_observations(
                    observation_id, observation_version, task_id, operation_id,
                    authority_domain, effect_identity, effect_request_hash,
                    external_object_id, content_hash, observation_json, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation.observation_id,
                    observation.observation_version,
                    observation.task_id,
                    observation.operation_id,
                    observation.authority_domain,
                    observation.effect_identity,
                    observation.effect_request_hash,
                    observation.external_object_id,
                    observation.content_hash,
                    observation.model_dump_json(),
                    _now().isoformat(),
                ),
            )
            return observation

    @staticmethod
    def _record_evidence_snapshot(
        connection: sqlite3.Connection,
        snapshot: EvidenceSnapshot,
    ) -> EvidenceSnapshot:
        connection.execute(
            """
            INSERT INTO phase3_evidence_snapshots(
                evidence_snapshot_id, task_id, attempt_id,
                collection_trigger_id, authoritative_versions_hash,
                collector_version, content_hash, snapshot_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(
                task_id, collection_trigger_id,
                authoritative_versions_hash, collector_version
            ) DO NOTHING
            """,
            (
                snapshot.evidence_snapshot_id,
                snapshot.task_id,
                snapshot.attempt_id,
                snapshot.collection_trigger_id,
                snapshot.authoritative_versions_hash,
                snapshot.collector_version,
                snapshot.content_hash,
                snapshot.model_dump_json(),
                snapshot.created_at.isoformat(),
            ),
        )
        existing = connection.execute(
            """
            SELECT snapshot_json FROM phase3_evidence_snapshots
            WHERE task_id = ? AND collection_trigger_id = ?
              AND authoritative_versions_hash = ? AND collector_version = ?
            """,
            (
                snapshot.task_id,
                snapshot.collection_trigger_id,
                snapshot.authoritative_versions_hash,
                snapshot.collector_version,
            ),
        ).fetchone()
        if existing is None:
            raise RuntimeError("EvidenceSnapshot persistence failed")
        persisted = EvidenceSnapshot.model_validate_json(existing["snapshot_json"])
        if persisted != snapshot:
            raise ValueError("EvidenceSnapshot identity conflict")
        return persisted

    def record_requirement_evaluations(
        self,
        task_id: str,
        evaluations: tuple[RequirementEvaluation, ...],
    ) -> None:
        with self.store.transaction() as connection:
            for evaluation in evaluations:
                connection.execute(
                    """
                    INSERT INTO phase3_requirement_evaluations(
                        evaluation_id, task_id, requirement_id,
                        evidence_snapshot_id, status, evaluation_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(evaluation_id) DO NOTHING
                    """,
                    (
                        evaluation.evaluation_id,
                        task_id,
                        evaluation.requirement_id,
                        evaluation.evidence_snapshot_id,
                        evaluation.status.value,
                        evaluation.model_dump_json(),
                        _now().isoformat(),
                    ),
                )
                existing = connection.execute(
                    """
                    SELECT task_id, evaluation_json
                    FROM phase3_requirement_evaluations
                    WHERE evaluation_id = ?
                    """,
                    (evaluation.evaluation_id,),
                ).fetchone()
                if (
                    existing is None
                    or existing["task_id"] != task_id
                    or RequirementEvaluation.model_validate_json(
                        existing["evaluation_json"]
                    )
                    != evaluation
                ):
                    raise ValueError("RequirementEvaluation identity conflict")

    def record_rule_evaluations(
        self,
        task_id: str,
        evaluations: tuple[RuleEvaluation, ...],
    ) -> None:
        with self.store.transaction() as connection:
            for evaluation in evaluations:
                connection.execute(
                    """
                    INSERT INTO phase3_rule_evaluations(
                        rule_evaluation_id, task_id, rule_id, rule_version,
                        decision_point, evidence_snapshot_id, status,
                        evaluation_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rule_evaluation_id) DO NOTHING
                    """,
                    (
                        evaluation.rule_evaluation_id,
                        task_id,
                        evaluation.rule_id,
                        evaluation.rule_version,
                        evaluation.decision_point.value,
                        evaluation.evidence_snapshot_id,
                        evaluation.status.value,
                        evaluation.model_dump_json(),
                        _now().isoformat(),
                    ),
                )
                existing = connection.execute(
                    """
                    SELECT task_id, evaluation_json
                    FROM phase3_rule_evaluations
                    WHERE rule_evaluation_id = ?
                    """,
                    (evaluation.rule_evaluation_id,),
                ).fetchone()
                if (
                    existing is None
                    or existing["task_id"] != task_id
                    or RuleEvaluation.model_validate_json(
                        existing["evaluation_json"]
                    )
                    != evaluation
                ):
                    raise ValueError("RuleEvaluation identity conflict")

    def record_completion_validation(
        self, task_id: str, validation: CompletionValidationResult
    ) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO phase3_completion_validations(
                    completion_validation_id, task_id, status,
                    validation_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(completion_validation_id) DO NOTHING
                """,
                (
                    validation.completion_validation_id,
                    task_id,
                    validation.status.value,
                    validation.model_dump_json(),
                    _now().isoformat(),
                ),
            )
            existing = connection.execute(
                """
                SELECT task_id, validation_json
                FROM phase3_completion_validations
                WHERE completion_validation_id = ?
                """,
                (validation.completion_validation_id,),
            ).fetchone()
            if (
                existing is None
                or existing["task_id"] != task_id
                or CompletionValidationResult.model_validate_json(
                    existing["validation_json"]
                )
                != validation
            ):
                raise ValueError("CompletionValidation identity conflict")

    def record_policy_decision(self, decision: PolicyDecision) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO phase3_policy_decisions(
                    decision_id, decision_key, task_id, attempt_id,
                    decision_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_key) DO NOTHING
                """,
                (
                    decision.decision_id,
                    decision.decision_key,
                    decision.task_id,
                    decision.attempt_id,
                    decision.model_dump_json(),
                    _now().isoformat(),
                ),
            )
            existing = connection.execute(
                """
                SELECT decision_json FROM phase3_policy_decisions
                WHERE decision_key = ?
                """,
                (decision.decision_key,),
            ).fetchone()
            if (
                existing is None
                or PolicyDecision.model_validate_json(existing[0]) != decision
            ):
                raise ValueError("PolicyDecision identity conflict")

    def apply_decision(self, decision_id: str) -> GovernanceApplicationResult:
        """Compatibility surface that preserves the Runtime ownership boundary."""

        raise RuntimeError("PolicyDecision must be applied by TaskRuntime")

    @staticmethod
    def _publish_operation_fact(
        connection: sqlite3.Connection,
        operation: ExternalOperation,
        *,
        fact_type: str,
    ) -> None:
        occurred = _now()
        fact_id = (
            f"fact:{operation.operation_id}:{fact_type}:v{operation.version}"
        )
        fact = ReliabilityFact(
            fact_id=fact_id,
            fact_type=fact_type,
            task_id=operation.task_id,
            attempt_id=operation.attempt_id,
            execution_id=operation.execution_id,
            source=FactSource(
                kind="external_operation_store", name="astra", version="1"
            ),
            authority_scope="external_operation",
            source_event_id=f"{operation.operation_id}:{fact_type}:v{operation.version}",
            subject_ref=SubjectRef(
                type="external_operation",
                id=operation.operation_id,
                version=operation.version,
                authority_domain="astra",
            ),
            occurred_at=occurred,
            evidence_refs=(operation.operation_id,),
            attributes={
                "effect_identity": operation.effect_identity,
                "effect_request_hash": operation.effect_request_hash,
                "status": operation.status.value,
            },
        )
        RuntimeGovernanceCore._insert_fact_outbox(connection, fact)

    @staticmethod
    def _insert_fact_outbox(
        connection: sqlite3.Connection, fact: ReliabilityFact
    ) -> None:
        outbox = OutboxMessage(
            outbox_id="outbox:" + fact.fact_id,
            fact_id=fact.fact_id,
            topic="astra.phase3.reliability_fact",
            payload=fact.model_dump(mode="json", exclude_none=True),
            created_at=fact.recorded_at,
        )
        connection.execute(
            """
            INSERT INTO phase3_reliability_facts(
                fact_id, fact_type, task_id, attempt_id, source_kind,
                source_name, source_event_id, fact_json, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact.fact_id,
                fact.fact_type,
                fact.task_id,
                fact.attempt_id,
                fact.source.kind,
                fact.source.name,
                fact.source_event_id,
                fact.model_dump_json(),
                fact.recorded_at.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO phase3_outbox(
                outbox_id, fact_id, topic, payload_json, delivery_status,
                delivery_attempts, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                outbox.outbox_id,
                outbox.fact_id,
                outbox.topic,
                _json(outbox.payload),
                outbox.delivery_status,
                outbox.delivery_attempts,
                outbox.created_at.isoformat(),
            ),
        )

def validate_governance_round2_fixture(
    fixture: Mapping[str, Any],
) -> "Round2ValidationReport":
    """Run the non-Production contract fixture validator.

    This helper deliberately does not assemble or advance the durable Runtime
    lifecycle and must never be used as Batch 1 Production evidence.
    """

    from .round2 import validate_round2_fixture

    return validate_round2_fixture(fixture)
