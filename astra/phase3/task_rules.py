"""Pure, versioned Task Rules evaluated from frozen governance evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from enum import Enum
from typing import Protocol

from pydantic import Field

from .canonical import sha256_digest
from .completion import CompletionValidationResult, EvidenceSnapshot
from .effects import ExternalOperationStatus
from .policy import DecisionPoint
from .task_contract import FrozenContractModel


class RuleStatus(str, Enum):
    PASS = "pass"
    FINDING = "finding"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not_applicable"
    RULE_ERROR = "rule_error"


class RuleFinding(FrozenContractModel):
    finding_id: str
    finding_type: str
    severity: str
    message: str
    fact_refs: tuple[str, ...] = ()
    record_refs: tuple[str, ...] = ()
    requirement_refs: tuple[str, ...] = ()


class RuleEvaluation(FrozenContractModel):
    rule_evaluation_id: str
    rule_id: str
    rule_version: str
    decision_point: DecisionPoint
    evidence_snapshot_id: str
    task_version: int = Field(ge=0)
    attempt_version: int = Field(ge=0)
    status: RuleStatus
    findings: tuple[RuleFinding, ...] = ()
    message: str | None = None


class TaskRuleContext(FrozenContractModel):
    decision_point: DecisionPoint
    evidence_snapshot: EvidenceSnapshot
    completion_validation: CompletionValidationResult | None = None
    supporting_facts: tuple[Mapping[str, object], ...] = ()


class TaskRule(Protocol):
    rule_id: str
    rule_version: str

    def evaluate(self, context: TaskRuleContext) -> RuleEvaluation: ...


def _evaluation(
    rule: TaskRule,
    context: TaskRuleContext,
    status: RuleStatus,
    *,
    findings: tuple[RuleFinding, ...] = (),
    message: str | None = None,
) -> RuleEvaluation:
    snapshot = context.evidence_snapshot
    identity = {
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "decision_point": context.decision_point.value,
        "evidence_snapshot_id": snapshot.evidence_snapshot_id,
        "task_version": snapshot.task_version,
        "attempt_version": snapshot.attempt_version,
    }
    return RuleEvaluation(
        rule_evaluation_id="rule-eval:" + sha256_digest(identity),
        rule_id=rule.rule_id,
        rule_version=rule.rule_version,
        decision_point=context.decision_point,
        evidence_snapshot_id=snapshot.evidence_snapshot_id,
        task_version=snapshot.task_version,
        attempt_version=snapshot.attempt_version,
        status=status,
        findings=findings,
        message=message,
    )


def _finding(
    rule: TaskRule,
    context: TaskRuleContext,
    *,
    finding_type: str,
    severity: str,
    message: str,
    record_refs: tuple[str, ...] = (),
    requirement_refs: tuple[str, ...] = (),
) -> RuleFinding:
    snapshot = context.evidence_snapshot
    payload = {
        "rule_id": rule.rule_id,
        "rule_version": rule.rule_version,
        "evidence_snapshot_id": snapshot.evidence_snapshot_id,
        "finding_type": finding_type,
        "record_refs": record_refs,
        "requirement_refs": requirement_refs,
    }
    return RuleFinding(
        finding_id="finding:" + sha256_digest(payload),
        finding_type=finding_type,
        severity=severity,
        message=message,
        fact_refs=tuple(
            str(fact["fact_id"])
            for fact in context.supporting_facts
            if fact.get("fact_id") is not None
        ),
        record_refs=record_refs,
        requirement_refs=requirement_refs,
    )


class CrossAttemptProgressRule:
    rule_id = "astra.cross_attempt_progress"
    rule_version = "1"

    def evaluate(self, context: TaskRuleContext) -> RuleEvaluation:
        snapshot = context.evidence_snapshot
        if int(snapshot.runtime_counters.get("attempt_count", 0)) < 2:
            return _evaluation(self, context, RuleStatus.NOT_APPLICABLE)
        resolved_interaction = any(
            state == "resolved" for state in snapshot.interaction_states.values()
        )
        confirmed_operation = any(
            operation.status == ExternalOperationStatus.CONFIRMED
            for operation in snapshot.external_operations
        )
        progress = bool(
            snapshot.receipt_refs
            or snapshot.business_observations
            or snapshot.completion_refs
            or resolved_interaction
            or confirmed_operation
        )
        if progress:
            return _evaluation(self, context, RuleStatus.PASS)
        unmet = (
            context.completion_validation.unmet_requirement_ids
            if context.completion_validation is not None
            else ()
        )
        finding = _finding(
            self,
            context,
            finding_type="cross_attempt_no_progress",
            severity="medium",
            message="Multiple Attempts produced no new authoritative task-level progress.",
            record_refs=tuple(snapshot.execution_refs),
            requirement_refs=tuple(unmet),
        )
        return _evaluation(self, context, RuleStatus.FINDING, findings=(finding,))


class DuplicateSideEffectRule:
    rule_id = "astra.duplicate_side_effect"
    rule_version = "1"

    def evaluate(self, context: TaskRuleContext) -> RuleEvaluation:
        confirmed = tuple(
            operation
            for operation in context.evidence_snapshot.external_operations
            if operation.status == ExternalOperationStatus.CONFIRMED
        )
        if not confirmed:
            return _evaluation(self, context, RuleStatus.NOT_APPLICABLE)
        identities = Counter(
            (operation.authority_domain, operation.effect_identity)
            for operation in confirmed
        )
        business_objects = Counter(
            (operation.authority_domain, operation.external_operation_id)
            for operation in confirmed
            if operation.external_operation_id is not None
        )
        duplicated_ids = {
            identity for identity, count in identities.items() if count > 1
        }
        duplicated_objects = {
            identity for identity, count in business_objects.items() if count > 1
        }
        offenders = tuple(
            operation.operation_id
            for operation in confirmed
            if (operation.authority_domain, operation.effect_identity) in duplicated_ids
            or (operation.authority_domain, operation.external_operation_id)
            in duplicated_objects
        )
        if not offenders:
            return _evaluation(self, context, RuleStatus.PASS)
        finding = _finding(
            self,
            context,
            finding_type="duplicate_side_effect",
            severity="high",
            message="More than one confirmed operation maps to the same governed effect or business object.",
            record_refs=offenders,
        )
        return _evaluation(self, context, RuleStatus.FINDING, findings=(finding,))


class LateStateAffectingRecordRule:
    rule_id = "astra.late_state_affecting_record"
    rule_version = "1"

    def evaluate(self, context: TaskRuleContext) -> RuleEvaluation:
        state = str(
            context.evidence_snapshot.authoritative_versions.get("task", {}).get(
                "state", ""
            )
        )
        if state not in {"succeeded", "failed", "cancelled"}:
            return _evaluation(self, context, RuleStatus.NOT_APPLICABLE)
        return _evaluation(
            self,
            context,
            RuleStatus.INDETERMINATE,
            message="Terminal-state timing is not present in this EvidenceSnapshot.",
        )


class RecoveryDivergenceRule:
    rule_id = "astra.recovery_divergence"
    rule_version = "1"

    def evaluate(self, context: TaskRuleContext) -> RuleEvaluation:
        snapshot = context.evidence_snapshot
        if int(snapshot.runtime_counters.get("attempt_count", 0)) < 2:
            return _evaluation(self, context, RuleStatus.NOT_APPLICABLE)
        confirmed = tuple(
            operation
            for operation in snapshot.external_operations
            if operation.status == ExternalOperationStatus.CONFIRMED
        )
        if not confirmed:
            return _evaluation(self, context, RuleStatus.PASS)
        observed_operations = {
            observation.operation_id for observation in snapshot.business_observations
        }
        missing = tuple(
            operation.operation_id
            for operation in confirmed
            if operation.operation_id not in observed_operations
        )
        if missing:
            return _evaluation(
                self,
                context,
                RuleStatus.INDETERMINATE,
                message="Recovery evidence is incomplete for confirmed operations: "
                + ", ".join(missing),
            )
        return _evaluation(self, context, RuleStatus.PASS)


class ReceiptBusinessStateMismatchRule:
    rule_id = "astra.receipt_business_state_mismatch"
    rule_version = "1"

    def evaluate(self, context: TaskRuleContext) -> RuleEvaluation:
        snapshot = context.evidence_snapshot
        confirmed = tuple(
            operation
            for operation in snapshot.external_operations
            if operation.status == ExternalOperationStatus.CONFIRMED
        )
        if not confirmed:
            return _evaluation(self, context, RuleStatus.NOT_APPLICABLE)
        observations = {
            observation.operation_id: observation
            for observation in snapshot.business_observations
        }
        missing = tuple(
            operation.operation_id
            for operation in confirmed
            if operation.operation_id not in observations
        )
        if missing:
            return _evaluation(
                self,
                context,
                RuleStatus.INDETERMINATE,
                message="No authoritative business Observation exists for: "
                + ", ".join(missing),
            )
        mismatched = tuple(
            operation.operation_id
            for operation in confirmed
            if observations[operation.operation_id].effect_identity
            != operation.effect_identity
            or observations[operation.operation_id].effect_request_hash
            != operation.effect_request_hash
            or observations[operation.operation_id].external_object_id
            != operation.external_operation_id
        )
        if not mismatched:
            return _evaluation(self, context, RuleStatus.PASS)
        finding = _finding(
            self,
            context,
            finding_type="receipt_business_state_mismatch",
            severity="high",
            message="Confirmed operation identity conflicts with authoritative business state.",
            record_refs=mismatched,
        )
        return _evaluation(self, context, RuleStatus.FINDING, findings=(finding,))


class TaskRuleRegistry:
    """Version-keyed registry and central-switch-free pure Rule runner."""

    def __init__(self) -> None:
        self._rules: dict[tuple[str, str], TaskRule] = {}

    def register(self, rule: TaskRule) -> None:
        key = (rule.rule_id, rule.rule_version)
        if key in self._rules:
            raise ValueError(f"Task Rule already registered: {key!r}")
        self._rules[key] = rule

    def resolve(self, rule_id: str, rule_version: str) -> TaskRule:
        try:
            return self._rules[(rule_id, rule_version)]
        except KeyError as exc:
            raise KeyError(f"Unknown Task Rule: {rule_id}@{rule_version}") from exc

    def evaluate_all(self, context: TaskRuleContext) -> tuple[RuleEvaluation, ...]:
        evaluations: list[RuleEvaluation] = []
        for key in sorted(self._rules):
            rule = self._rules[key]
            try:
                evaluations.append(rule.evaluate(context))
            except BaseException as exc:
                evaluations.append(
                    _evaluation(
                        rule,
                        context,
                        RuleStatus.RULE_ERROR,
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
        return tuple(evaluations)

    @property
    def registered_refs(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._rules))


FROZEN_TASK_RULES: tuple[TaskRule, ...] = (
    CrossAttemptProgressRule(),
    DuplicateSideEffectRule(),
    LateStateAffectingRecordRule(),
    RecoveryDivergenceRule(),
    ReceiptBusinessStateMismatchRule(),
)


def build_production_task_rule_registry() -> TaskRuleRegistry:
    registry = TaskRuleRegistry()
    for rule in FROZEN_TASK_RULES:
        registry.register(rule)
    return registry
