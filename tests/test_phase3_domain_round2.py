from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from astra.phase3 import (
    ApprovalBindingCode,
    ApprovalDecision,
    ApprovalRequirementRef,
    ApprovalResolution,
    PolicyAction,
    Round2Path,
    TaskContract,
    TaskState,
    build_canonical_effect_request,
    evaluate_round2_contract_chain,
    verify_approval_binding,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "phase3_contract_round2.json"
FIXTURES = json.loads(FIXTURE_PATH.read_text())
CASES = [
    pytest.param(scenario, path_name, path, id=f"{scenario['scenario_id']}:{path_name}")
    for scenario in FIXTURES["scenarios"]
    for path_name, path in FIXTURES["paths"].items()
]


@pytest.mark.parametrize(("scenario", "path_name", "path"), CASES)
def test_production_domain_chain_matches_frozen_round2_fixture(
    scenario: dict, path_name: str, path: dict
):
    result = evaluate_round2_contract_chain(
        task_contract=scenario["task_contract"],
        normalized_parameters=scenario["normalized_parameters"],
        normalizer_id=scenario["normalizer"]["normalizer_id"],
        normalizer_version=scenario["normalizer"]["normalizer_version"],
        completion_requirement=FIXTURES["completion_requirement"],
        path=Round2Path.model_validate(path),
    )

    assert result.requirement_evaluation.status.value == path["expected_evaluation"]
    assert result.policy_action.value == path["expected_policy_action"]
    assert result.decision_application.value == path["expected_application"]
    assert result.task_contract.contract_hash == (
        result.evidence_snapshot.task_contract_ref.contract_hash
    )
    if result.external_operation is not None:
        assert result.external_operation.effect_identity == result.effect_identity
        assert (
            result.external_operation.effect_request_hash
            == result.effect_request_hash
        )


def test_task_contract_rejects_workflow_fields_anywhere():
    template = deepcopy(FIXTURES["scenarios"][0]["task_contract"])
    template["constraints"][0]["configuration"]["next_tool"] = "forbidden"
    with pytest.raises(ValidationError, match="forbidden workflow fields"):
        TaskContract.materialize(template)


def test_approval_is_bound_to_exact_effect_identity():
    scenario = FIXTURES["scenarios"][1]
    contract = TaskContract.materialize(scenario["task_contract"])
    intent = contract.authorized_effects[0]
    effect = build_canonical_effect_request(
        contract,
        effect_intent_ref=intent.effect_intent_id,
        normalized_parameters=scenario["normalized_parameters"],
        normalizer_id=scenario["normalizer"]["normalizer_id"],
        normalizer_version=scenario["normalizer"]["normalizer_version"],
    )
    requirement = contract.approval_requirement(intent.approval_requirement_ref or "")
    resolution = ApprovalResolution(
        approval_resolution_id="resolution-1",
        approval_request_id="request-1",
        interaction_id="interaction-1",
        decision=ApprovalDecision.APPROVED,
        task_contract_ref=contract.ref,
        effect_identity=effect.effect_identity + "-different",
        effect_request_hash=effect.effect_request_hash,
        approval_requirement_ref=ApprovalRequirementRef.parse(requirement.ref),
        approver_subject_ref="manager-1",
        approver_policy_ref=requirement.approver_policy_ref,
        permission_scope="execute_effect",
        resolved_at="2026-07-20T00:00:00Z",
        valid_from="2026-07-20T00:00:00Z",
        expires_at="2026-07-21T00:00:00Z",
        usage_semantics=requirement.usage_semantics,
    )
    result = verify_approval_binding(
        contract,
        effect,
        resolution,
        now=resolution.valid_from,
    )
    assert result.matched is False
    assert result.code == ApprovalBindingCode.EFFECT_MISMATCH


def test_frozen_core_surfaces_are_not_expanded():
    assert {item.value for item in TaskState} == {
        "pending",
        "running",
        "waiting_input",
        "waiting_approval",
        "reconciling",
        "succeeded",
        "failed",
        "cancelled",
    }
    assert {item.value for item in PolicyAction} == {
        "complete",
        "continue_with_feedback",
        "start_new_attempt",
        "request_input",
        "request_approval",
        "reconcile",
        "fail",
        "escalate",
    }
