from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from astra.phase3 import (
    ApprovalBindingCode,
    ApprovalDecision,
    ApprovalRequirementRef,
    ApprovalResolution,
    GovernanceStore,
    PolicyAction,
    Round2Path,
    RuntimeGovernanceCore,
    TaskContract,
    TaskState,
    build_canonical_effect_request,
    evaluate_round2_contract_chain,
    verify_approval_binding,
)
from astra.runtime import Phase2Runtime
from astra.storage import AstraStore


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


def test_phase3_cli_is_the_fixture_validation_entrypoint():
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "astra.phase3",
            "validate-round2",
            "--fixture",
            str(FIXTURE_PATH),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["valid"] is True
    assert report["case_count"] == 35
    assert report["passed_count"] == 35

    governance = subprocess.run(
        [
            sys.executable,
            "-m",
            "astra.phase3",
            "validate-governance-round2",
            "--fixture",
            str(FIXTURE_PATH),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert governance.returncode == 0, governance.stdout + governance.stderr
    governance_report = json.loads(governance.stdout)
    assert governance_report["valid"] is True
    assert governance_report["passed_count"] == 35


def test_governance_core_is_composed_into_existing_phase2_runtime():
    scenario = FIXTURES["scenarios"][0]
    path = FIXTURES["paths"]["success"]
    chain = evaluate_round2_contract_chain(
        task_contract=scenario["task_contract"],
        normalized_parameters=scenario["normalized_parameters"],
        normalizer_id=scenario["normalizer"]["normalizer_id"],
        normalizer_version=scenario["normalizer"]["normalizer_version"],
        path=path,
    )
    phase2_store = AstraStore()
    governance_store = GovernanceStore.from_astra_store(phase2_store)
    governance_core = RuntimeGovernanceCore(governance_store)
    runtime = Phase2Runtime(phase2_store, governance_core=governance_core)
    try:
        governance_core.create_task(
            chain.task_contract,
            task_id="task-round2",
            attempt_id="attempt-round2",
            task_version=12,
            attempt_version=5,
        )
        assert chain.external_operation is not None
        assert chain.canonical_effect_request is not None
        assert chain.approval_resolution is not None
        governance_core.record_approval_resolution(
            "task-round2", chain.approval_resolution
        )
        prepared, created = governance_core.prepare_external_operation(
            chain.canonical_effect_request,
            task_id="task-round2",
            attempt_id="attempt-round2",
            execution_id="execution-round2",
            approval_resolution_id=(
                chain.approval_resolution.approval_resolution_id
            ),
            operation_id=chain.external_operation.operation_id,
            now=chain.external_operation.created_at,
        )
        assert created is True
        replayed_operation, replay_created = (
            governance_core.prepare_external_operation(
                chain.canonical_effect_request,
                task_id="task-round2",
                attempt_id="attempt-round2",
                execution_id="execution-round2-retry",
                approval_resolution_id=(
                    chain.approval_resolution.approval_resolution_id
                ),
                operation_id="must-not-be-created",
                now=chain.external_operation.created_at,
            )
        )
        assert replay_created is False
        assert replayed_operation.operation_id == prepared.operation_id
        governance_core.transition_external_operation(
            prepared.operation_id, chain.external_operation.status
        )
        governance_core.record_completion_validation(
            "task-round2", chain.completion_validation
        )
        governance_core.record_policy_decision(chain.policy_decision)

        applied = runtime.apply_governance_decision(
            chain.policy_decision.decision_id
        )
        replayed = runtime.apply_governance_decision(
            chain.policy_decision.decision_id
        )
        assert applied.application.value == "applied"
        assert applied.task_state.value == "succeeded"
        assert replayed.application.value == "applied"
        assert governance_store.query_one(
            "SELECT usage_count FROM phase3_approval_resolutions"
        )[0] == 1
        assert governance_store.query_one(
            "SELECT COUNT(*) FROM phase3_reliability_facts"
        )[0] == 3
        assert governance_store.query_one(
            "SELECT COUNT(*) FROM phase3_outbox"
        )[0] == 3
    finally:
        governance_store.close()
        phase2_store.close()
