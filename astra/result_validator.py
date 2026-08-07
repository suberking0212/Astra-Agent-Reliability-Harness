"""Platform result validation plus small, registered domain evaluators."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from uuid import uuid4

from .domain import ResultReceipt, RuntimeInvocation
from .phase3.approval import ApprovalDecision, ApprovalResolution
from .phase3.effects import CanonicalEffectRequest, ExternalOperation, ExternalOperationStatus
from .phase3.task_contract import ExecutionType
from .storage import AstraStore


class ResultEvaluator(Protocol):
    evaluator_id: str
    evaluator_version: str

    def handles(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> bool: ...

    def evaluate(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, bool]: ...


class ResultValidator(Protocol):
    store: AstraStore

    def validate(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        evidence_refs: Sequence[str],
        receipt_refs: Sequence[str],
    ) -> ResultReceipt: ...


class ResultEvaluatorRegistry:
    """Simple static registry; ambiguous ownership fails closed."""

    def __init__(self) -> None:
        self._evaluators: dict[tuple[str, str], ResultEvaluator] = {}

    def register(self, evaluator: ResultEvaluator) -> None:
        key = (evaluator.evaluator_id, evaluator.evaluator_version)
        if key in self._evaluators:
            raise ValueError(f"Result evaluator already registered: {key!r}")
        self._evaluators[key] = evaluator

    @property
    def registered_refs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._evaluators))

    def matching(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
    ) -> tuple[ResultEvaluator, ...]:
        return tuple(
            evaluator
            for evaluator in self._evaluators.values()
            if evaluator.handles(invocation, outcome, receipts)
        )


class MinimalResultValidator:
    """Validate only platform invariants, delegating domain truth to evaluators."""

    def __init__(
        self,
        store: AstraStore,
        evaluator_registry: ResultEvaluatorRegistry | None = None,
    ) -> None:
        self.store = store
        self.evaluator_registry = evaluator_registry or ResultEvaluatorRegistry()

    def validate(
        self,
        invocation: RuntimeInvocation,
        outcome: Mapping[str, Any],
        evidence_refs: Sequence[str],
        receipt_refs: Sequence[str],
    ) -> ResultReceipt:
        receipt_rows = self.store.get_execution_receipts(receipt_refs)
        receipts = tuple(dict(row) for row in receipt_rows)
        receipt_ids = {str(row["receipt_id"]) for row in receipts}
        requested_receipt_ids = set(receipt_refs)
        granted_receipt_ids = set(invocation.evidence_receipt_ids)

        governed_terminal = self._governed_terminal_evidence(invocation)
        checks: dict[str, bool] = {
            "receipt_refs_resolvable": receipt_ids == requested_receipt_ids,
            "receipt_refs_owned_by_task": all(
                row["task_id"] == invocation.task_id for row in receipts
            ),
            "receipt_refs_owned_by_active_attempt": all(
                row["attempt_id"] == invocation.attempt_id for row in receipts
            ),
            "receipt_refs_in_evidence_grant": all(
                row["execution_id"] == invocation.execution_id
                or str(row["receipt_id"]) in granted_receipt_ids
                for row in receipts
            ),
            "evidence_refs_backed_by_receipts": set(evidence_refs).issubset(
                receipt_ids
            ),
            "no_unresolved_interaction": self.store.pending_interaction(
                invocation.execution_id
            )
            is None,
            **governed_terminal["checks"],
        }
        if (
            invocation.task_contract.get("execution_type")
            == ExecutionType.DIRECT_RESPONSE.value
        ):
            assistant_output = outcome.get("assistant_output")
            checks["direct_response_assistant_output_present"] = bool(
                isinstance(assistant_output, str) and assistant_output.strip()
            )
        platform_errors = [name for name, passed in checks.items() if not passed]

        evaluator_error = False
        if governed_terminal["denied"] or governed_terminal["failed"]:
            evaluators = ()
        else:
            try:
                evaluators = self.evaluator_registry.matching(
                    invocation, outcome, receipts
                )
            except Exception:
                evaluators = ()
                evaluator_error = True
        contract = invocation.task_contract
        domain_required = bool(
            contract.get("resolved_tools")
            or contract.get("authorized_effects")
            or any(bool(row.get("side_effect")) for row in receipts)
        ) and not (governed_terminal["denied"] or governed_terminal["failed"])
        checks["domain_result_evaluator_required"] = domain_required
        checks["domain_result_evaluated"] = len(evaluators) == 1
        domain_errors: list[str] = []
        if evaluator_error:
            domain_errors.append("domain_result_evaluator_error")
        elif len(evaluators) > 1:
            domain_errors.append("domain_result_evaluator_ambiguous")
        elif len(evaluators) == 1:
            try:
                raw_domain_checks = dict(
                    evaluators[0].evaluate(invocation, outcome, receipts)
                )
                if any(
                    not isinstance(passed, bool)
                    for passed in raw_domain_checks.values()
                ):
                    raise TypeError("Result evaluator checks must be boolean")
                checks.update(raw_domain_checks)
                domain_errors.extend(
                    name
                    for name, passed in raw_domain_checks.items()
                    if not passed
                )
            except Exception:
                checks["domain_result_evaluated"] = False
                domain_errors.append("domain_result_evaluator_error")
        elif domain_required:
            domain_errors.append("domain_result_evaluator_missing")

        errors = tuple(platform_errors + domain_errors)
        receipt = ResultReceipt(
            receipt_id=str(uuid4()),
            execution_id=invocation.execution_id,
            task_id=invocation.task_id,
            valid=not errors,
            outcome=dict(outcome),
            evidence_refs=tuple(evidence_refs),
            receipt_refs=tuple(receipt_refs),
            checks=checks,
            errors=errors,
        )
        self.store.add_result_receipt(receipt)
        return receipt

    def _governed_terminal_evidence(
        self, invocation: RuntimeInvocation
    ) -> Mapping[str, Any]:
        """Resolve denial/failure only from persisted exact-effect authority."""

        checks: dict[str, bool] = {}
        denied = False
        if invocation.run_request_id is not None:
            row = self.store.query_one(
                """
                SELECT interaction.resolution_json
                FROM phase4_run_requests AS request
                JOIN interactions AS interaction
                  ON interaction.interaction_id = request.source_interaction_id
                WHERE request.run_request_id = ?
                  AND request.task_id = ?
                  AND request.attempt_id = ?
                  AND interaction.kind = 'approval'
                  AND interaction.status = 'resolved'
                """,
                (
                    invocation.run_request_id,
                    invocation.task_id,
                    invocation.attempt_id,
                ),
            )
            if row is not None and row["resolution_json"]:
                try:
                    resolution = ApprovalResolution.model_validate_json(
                        str(row["resolution_json"])
                    )
                except Exception:
                    resolution = None
                if (
                    resolution is not None
                    and resolution.decision == ApprovalDecision.DENIED
                ):
                    persisted = self.store.query_one(
                        """
                        SELECT resolution_json
                        FROM phase3_approval_resolutions
                        WHERE approval_resolution_id = ?
                          AND task_id = ?
                          AND effect_identity = ?
                          AND effect_request_hash = ?
                          AND decision = 'denied'
                        """,
                        (
                            resolution.approval_resolution_id,
                            invocation.task_id,
                            resolution.effect_identity,
                            resolution.effect_request_hash,
                        ),
                    )
                    canonical = self.store.query_one(
                        """
                        SELECT request_json
                        FROM phase3_canonical_effect_requests
                        WHERE task_id = ? AND effect_identity = ?
                          AND effect_request_hash = ?
                        """,
                        (
                            invocation.task_id,
                            resolution.effect_identity,
                            resolution.effect_request_hash,
                        ),
                    )
                    operation = self.store.query_one(
                        """
                        SELECT status FROM phase3_external_operations
                        WHERE task_id = ? AND effect_identity = ?
                          AND effect_request_hash = ?
                        """,
                        (
                            invocation.task_id,
                            resolution.effect_identity,
                            resolution.effect_request_hash,
                        ),
                    )
                    checks["authoritative_denial_resolved"] = bool(
                        persisted is not None
                        and ApprovalResolution.model_validate_json(
                            persisted["resolution_json"]
                        )
                        == resolution
                    )
                    checks["denied_effect_identity_persisted"] = bool(
                        canonical is not None
                        and CanonicalEffectRequest.model_validate_json(
                            canonical["request_json"]
                        ).effect_request_hash
                        == resolution.effect_request_hash
                    )
                    checks["denied_effect_not_executed"] = operation is None
                    denied = all(checks.values())

        failed = False
        failed_rows = self.store.query_all(
            """
            SELECT operation_json
            FROM phase3_external_operations
            WHERE task_id = ? AND attempt_id = ? AND status = 'failed'
            ORDER BY operation_id
            """,
            (invocation.task_id, invocation.attempt_id),
        )
        if failed_rows:
            structured_failures: list[bool] = []
            for row in failed_rows:
                try:
                    operation = ExternalOperation.model_validate_json(
                        row["operation_json"]
                    )
                except Exception:
                    structured_failures.append(False)
                    continue
                canonical = self.store.query_one(
                    """
                    SELECT request_json
                    FROM phase3_canonical_effect_requests
                    WHERE task_id = ? AND effect_identity = ?
                      AND effect_request_hash = ?
                    """,
                    (
                        invocation.task_id,
                        operation.effect_identity,
                        operation.effect_request_hash,
                    ),
                )
                structured_failures.append(
                    operation.status == ExternalOperationStatus.FAILED
                    and canonical is not None
                )
            checks["governed_effect_failure_persisted"] = bool(
                structured_failures and all(structured_failures)
            )
            failed = checks["governed_effect_failure_persisted"]
        return {"denied": denied, "failed": failed, "checks": checks}
