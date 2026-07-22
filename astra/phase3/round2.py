"""Executable pure-contract chain used by the frozen Round 2 scenarios."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import Field, computed_field

from .approval import (
    ApprovalDecision,
    ApprovalRequirementRef,
    ApprovalResolution,
    verify_approval_binding,
)
from .canonical import sha256_digest
from .completion import (
    AuthorizedEffectConfirmedEvaluator,
    BusinessObservation,
    CompletionContract,
    CompletionRequirement,
    CompletionStatus,
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
    build_canonical_effect_request,
)
from .policy import (
    DecisionApplication,
    DecisionContext,
    DecisionPoint,
    InteractionSpec,
    PolicyAction,
    PolicyDecision,
    PolicyFeedback,
    DecisionApplicationSnapshot,
    TaskState,
    apply_policy_decision,
    choose_policy_action,
    make_policy_decision,
)
from .task_contract import FrozenContractModel, TaskContract


_FIXED_TIME = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


class Round2Path(FrozenContractModel):
    input_complete: bool
    approval_status: str
    operation_status: str
    expected_evaluation: str | None = None
    expected_policy_action: str | None = None
    current_task_version_offset: int = 0
    expected_application: str | None = None


class Round2ChainResult(FrozenContractModel):
    task_contract: TaskContract
    canonical_effect_request: CanonicalEffectRequest | None
    approval_resolution: ApprovalResolution | None
    effect_identity: str | None
    effect_request_hash: str | None
    approval_matched: bool
    external_operation: ExternalOperation | None
    evidence_snapshot: EvidenceSnapshot
    requirement_evaluation: RequirementEvaluation
    completion_validation: CompletionValidationResult
    decision_context: DecisionContext
    policy_decision: PolicyDecision
    completion_status: CompletionStatus
    policy_action: PolicyAction
    decision_application: DecisionApplication


class Round2ValidationFailure(FrozenContractModel):
    scenario_id: str
    path_name: str
    field: str
    expected: str
    actual: str


class Round2ValidationReport(FrozenContractModel):
    fixture_schema_version: str
    scenario_count: int = Field(ge=0)
    path_count: int = Field(ge=0)
    case_count: int = Field(ge=0)
    passed_count: int = Field(ge=0)
    failures: tuple[Round2ValidationFailure, ...] = ()

    @computed_field
    @property
    def valid(self) -> bool:
        return not self.failures and self.passed_count == self.case_count


def evaluate_round2_contract_chain(
    *,
    task_contract: Mapping[str, Any],
    normalized_parameters: Mapping[str, Any],
    normalizer_id: str,
    normalizer_version: str,
    completion_requirement: Mapping[str, Any],
    path: Round2Path | Mapping[str, Any],
    task_id: str = "task-round2",
    attempt_id: str = "attempt-round2",
    execution_id: str = "execution-round2",
    expected_task_version: int = 12,
    expected_attempt_version: int = 5,
) -> Round2ChainResult:
    """Run the frozen contract/evidence/policy/CAS chain without executing a tool.

    This is deliberately not a workflow engine: it accepts already-collected
    path facts and evaluates their governance meaning.  Hermes still owns how
    any real task obtains those facts and which tools it calls.
    """

    path = path if isinstance(path, Round2Path) else Round2Path.model_validate(path)
    contract = TaskContract.materialize(task_contract)
    if len(contract.authorized_effects) != 1:
        raise ValueError("Round 2 fixtures require exactly one effect intent")
    intent = contract.authorized_effects[0]

    effect = None
    if path.input_complete:
        effect = build_canonical_effect_request(
            contract,
            effect_intent_ref=intent.effect_intent_id,
            normalized_parameters=normalized_parameters,
            normalizer_id=normalizer_id,
            normalizer_version=normalizer_version,
        )

    resolution = None
    if effect is not None and path.approval_status == "approved":
        requirement_ref = intent.approval_requirement_ref
        if requirement_ref is None:
            raise ValueError("Round 2 approved path requires an approval requirement")
        requirement = contract.approval_requirement(requirement_ref)
        resolution = ApprovalResolution(
            approval_resolution_id="approval-resolution:" + effect.effect_identity,
            approval_request_id="approval-request:" + effect.effect_identity,
            interaction_id="interaction:" + effect.effect_identity,
            decision=ApprovalDecision.APPROVED,
            task_contract_ref=contract.ref,
            effect_identity=effect.effect_identity,
            effect_request_hash=effect.effect_request_hash,
            approval_requirement_ref=ApprovalRequirementRef.parse(requirement_ref),
            approver_subject_ref="fixture-approver",
            approver_policy_ref=requirement.approver_policy_ref,
            permission_scope="execute_effect",
            resolved_at=_FIXED_TIME,
            valid_from=_FIXED_TIME,
            expires_at=_FIXED_TIME + timedelta(days=1),
            usage_semantics=requirement.usage_semantics,
        )
    binding = (
        verify_approval_binding(
            contract, effect, resolution, now=_FIXED_TIME
        )
        if effect is not None
        else None
    )
    approval_matched = bool(binding and binding.matched)

    operation = None
    operation_status = None
    if (
        effect is not None
        and approval_matched
        and path.operation_status != "not_started"
    ):
        operation_status = ExternalOperationStatus(path.operation_status)
        operation = ExternalOperation.from_effect(
            effect,
            operation_id="operation:" + sha256_digest(effect.effect_identity),
            task_id=task_id,
            attempt_id=attempt_id,
            execution_id=execution_id,
            status=operation_status,
            approval_ref=(
                resolution.approval_resolution_id if resolution else None
            ),
            created_at=_FIXED_TIME,
        )
        if operation_status == ExternalOperationStatus.CONFIRMED:
            operation = operation.model_copy(
                update={"external_operation_id": "round2-business-object"}
            )

    observation = None
    if operation is not None and operation_status == ExternalOperationStatus.CONFIRMED:
        observation = BusinessObservation.materialize(
            observation_id="observation:" + operation.operation_id,
            observation_version=1,
            task_id=task_id,
            operation_id=operation.operation_id,
            authority_domain=operation.authority_domain,
            effect_identity=operation.effect_identity,
            effect_request_hash=operation.effect_request_hash,
            external_object_id="round2-business-object",
            state=dict(normalized_parameters),
        )

    authoritative_versions = {
        "task": {"task_id": task_id, "version": expected_task_version},
        "contract": contract.ref.model_dump(mode="json"),
        "attempt": {"attempt_id": attempt_id, "version": expected_attempt_version},
        "executions": {execution_id: sha256_digest(path)},
        "interactions": {},
        "approvals": (
            {
                resolution.approval_resolution_id: sha256_digest(resolution)
            }
            if resolution
            else {}
        ),
        "external_operations": (
            {
                operation.operation_id: {
                    "version": operation.version,
                    "status": operation.status.value,
                    "content_hash": sha256_digest(operation),
                }
            }
            if operation
            else {}
        ),
        "canonical_effects": (
            {effect.effect_identity: effect.effect_request_hash} if effect else {}
        ),
        "receipts": {},
        "observations": (
            {
                observation.observation_id: {
                    "version": observation.observation_version,
                    "content_hash": observation.content_hash,
                }
            }
            if observation
            else {}
        ),
        "fact_watermark": 1,
        "completion_refs": {},
    }
    collection_trigger_id = "submitted-result:" + task_id
    authoritative_versions_hash, evidence_snapshot_id = (
        build_evidence_snapshot_identity(
            task_id=task_id,
            collection_trigger_id=collection_trigger_id,
            authoritative_versions=authoritative_versions,
            collector_version="1",
        )
    )

    snapshot = EvidenceSnapshot.materialize(
        evidence_snapshot_id=evidence_snapshot_id,
        task_id=task_id,
        attempt_id=attempt_id,
        collection_trigger_id=collection_trigger_id,
        authoritative_versions_hash=authoritative_versions_hash,
        authoritative_versions=authoritative_versions,
        task_version=expected_task_version,
        attempt_version=expected_attempt_version,
        task_contract_ref=contract.ref,
        execution_refs={execution_id: sha256_digest(path)},
        receipt_refs=(),
        receipt_hashes={},
        interaction_refs={},
        interaction_states={},
        external_operations=(operation,) if operation else (),
        canonical_effects=(effect,) if effect else (),
        effect_refs=(
            {effect.effect_identity: effect.effect_request_hash} if effect else {}
        ),
        approval_refs=(
            {resolution.approval_resolution_id: sha256_digest(resolution)}
            if resolution
            else {}
        ),
        business_observations=(observation,) if observation else (),
        business_observation_refs=(
            (observation.observation_id,)
            if observation is not None
            else ()
        ),
        completion_refs={},
        fact_watermark=1,
        collector_version="1",
        created_at=_FIXED_TIME,
    )

    submitted = SubmittedResult(
        submitted_result_id="submitted-result:" + task_id,
        task_id=task_id,
        attempt_id=attempt_id,
        execution_id=execution_id,
        outcome={},
    )
    requirement_payload = dict(completion_requirement)
    requirement_payload.setdefault(
        "configuration", {"effect_intent_ref": intent.effect_intent_id}
    )
    requirement_payload["required_evidence"] = tuple(
        dict.fromkeys(
            (
                *requirement_payload.get("required_evidence", ()),
                "external_operation",
                "business_state",
            )
        )
    )
    requirement = CompletionRequirement.model_validate(requirement_payload)
    completion_contract = CompletionContract(
        contract_id=contract.completion_contract_ref.contract_id,
        contract_version=contract.completion_contract_ref.contract_version,
        task_type=contract.task_type,
        requirements=(requirement,),
    )
    evaluator_registry = RequirementEvaluatorRegistry()
    evaluator_registry.register(AuthorizedEffectConfirmedEvaluator())
    evaluation = evaluator_registry.evaluate(requirement, submitted, snapshot)
    completion = aggregate_completion(
        completion_contract, submitted, snapshot, (evaluation,)
    )
    action, reason_code = choose_policy_action(
        input_complete=path.input_complete,
        approval_required=intent.approval_requirement_ref is not None,
        approval_matched=approval_matched,
        external_operation_status=operation_status,
        completion_status=completion.status,
    )

    context = DecisionContext.materialize(
        decision_context_id="context:" + snapshot.evidence_snapshot_id,
        decision_point=DecisionPoint.COMPLETION_VALIDATED,
        trigger_id=completion.completion_validation_id,
        task_id=task_id,
        task_version=expected_task_version,
        task_contract_ref=contract.ref,
        attempt_id=attempt_id,
        attempt_version=expected_attempt_version,
        evidence_snapshot_id=snapshot.evidence_snapshot_id,
        fact_watermark=snapshot.fact_watermark,
        completion_validation_id=completion.completion_validation_id,
        interaction_snapshot_version=0,
    )
    interaction_spec = None
    if action == PolicyAction.REQUEST_INPUT:
        interaction_spec = InteractionSpec(
            kind="user_input",
            reason_code=reason_code,
            required_information=("contract_required_input",),
        )
    elif action == PolicyAction.REQUEST_APPROVAL:
        if effect is None or intent.approval_requirement_ref is None:
            raise ValueError("Exact effect request is required before approval")
        interaction_spec = InteractionSpec(
            kind="approval",
            reason_code=reason_code,
            approval_requirement_ref=intent.approval_requirement_ref,
            effect_identity=effect.effect_identity,
            effect_request_hash=effect.effect_request_hash,
            permission_scope="execute_effect",
        )
    feedback = None
    if action == PolicyAction.CONTINUE_WITH_FEEDBACK:
        feedback = PolicyFeedback(
            unmet_requirements=(requirement.requirement_id,),
            suggestions=("Re-evaluate using the persisted evidence snapshot.",),
        )
    decision = make_policy_decision(
        context,
        action=action,
        reason_code=reason_code,
        evaluation_refs=(evaluation.evaluation_id,),
        feedback=feedback,
        interaction_spec=interaction_spec,
    )
    current = DecisionApplicationSnapshot(
        task_id=task_id,
        task_version=expected_task_version + path.current_task_version_offset,
        task_state=TaskState.RUNNING,
        attempt_id=attempt_id,
        attempt_version=expected_attempt_version,
        task_contract_ref=contract.ref,
        completion_validation_id=completion.completion_validation_id,
        required_external_operation_statuses=(
            (operation.status,) if operation is not None else ()
        ),
    )
    application = apply_policy_decision(
        decision, current, completion=completion
    )
    return Round2ChainResult(
        task_contract=contract,
        canonical_effect_request=effect,
        approval_resolution=resolution,
        effect_identity=effect.effect_identity if effect else None,
        effect_request_hash=effect.effect_request_hash if effect else None,
        approval_matched=approval_matched,
        external_operation=operation,
        evidence_snapshot=snapshot,
        requirement_evaluation=evaluation,
        completion_validation=completion,
        decision_context=context,
        policy_decision=decision,
        completion_status=completion.status,
        policy_action=action,
        decision_application=application,
    )


def validate_round2_fixture(fixture: Mapping[str, Any]) -> Round2ValidationReport:
    """Validate every table-driven path through the production domain chain."""

    scenarios = tuple(fixture.get("scenarios", ()))
    paths = dict(fixture.get("paths", {}))
    failures: list[Round2ValidationFailure] = []
    passed = 0
    for scenario in scenarios:
        scenario_id = str(scenario["scenario_id"])
        for path_name, raw_path in paths.items():
            path = Round2Path.model_validate(raw_path)
            result = evaluate_round2_contract_chain(
                task_contract=scenario["task_contract"],
                normalized_parameters=scenario["normalized_parameters"],
                normalizer_id=scenario["normalizer"]["normalizer_id"],
                normalizer_version=scenario["normalizer"]["normalizer_version"],
                completion_requirement=fixture["completion_requirement"],
                path=path,
                task_id=f"task-{scenario_id}",
                attempt_id=f"attempt-{scenario_id}",
                execution_id=f"execution-{scenario_id}",
            )
            checks = {
                "evaluation": (
                    path.expected_evaluation,
                    result.requirement_evaluation.status.value,
                ),
                "policy_action": (
                    path.expected_policy_action,
                    result.policy_action.value,
                ),
                "application": (
                    path.expected_application,
                    result.decision_application.value,
                ),
            }
            case_failed = False
            for field, (expected, actual) in checks.items():
                if expected is not None and expected != actual:
                    case_failed = True
                    failures.append(
                        Round2ValidationFailure(
                            scenario_id=scenario_id,
                            path_name=str(path_name),
                            field=field,
                            expected=expected,
                            actual=actual,
                        )
                    )
            if not case_failed:
                passed += 1
    return Round2ValidationReport(
        fixture_schema_version=str(fixture.get("fixture_schema_version", "")),
        scenario_count=len(scenarios),
        path_count=len(paths),
        case_count=len(scenarios) * len(paths),
        passed_count=passed,
        failures=tuple(failures),
    )
