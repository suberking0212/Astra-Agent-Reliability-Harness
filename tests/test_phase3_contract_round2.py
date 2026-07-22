from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from astra.phase3 import Round2Path, evaluate_round2_contract_chain

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "phase3_contract_round2.json"
ALLOWED_POLICY_ACTIONS = {
    "complete",
    "continue_with_feedback",
    "start_new_attempt",
    "request_input",
    "request_approval",
    "reconcile",
    "fail",
    "escalate",
}
TASK_STATES = {
    "pending",
    "running",
    "waiting_input",
    "waiting_approval",
    "reconciling",
    "succeeded",
    "failed",
    "cancelled",
}
FORBIDDEN_WORKFLOW_FIELDS = {
    "next_tool",
    "tool_arguments",
    "ordered_steps",
    "workflow",
    "business_plan",
}


def _load_fixtures() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _materialize_contract(template: dict) -> dict:
    contract = deepcopy(template)
    contract.pop("contract_hash", None)
    contract["contract_hash"] = _sha256(contract)
    return contract


def _effect_request(scenario: dict, contract: dict) -> dict:
    intent = contract["authorized_effects"][0]
    normalized_parameters = scenario["normalized_parameters"]
    parameters_hash = _sha256(normalized_parameters)
    request_without_hashes = {
        "effect_schema_version": "1",
        "normalizer_id": scenario["normalizer"]["normalizer_id"],
        "normalizer_version": scenario["normalizer"]["normalizer_version"],
        "task_contract_ref": {
            "contract_id": contract["contract_id"],
            "contract_version": contract["contract_version"],
            "contract_hash": contract["contract_hash"],
        },
        "effect_intent_ref": intent["effect_intent_id"],
        "authority_domain": intent["authority_domain"],
        "effect_type": intent["effect_type"],
        "effect_type_version": intent["effect_type_version"],
        "subject_ref": intent["subject_ref"],
        "normalized_parameters_hash": parameters_hash,
    }
    request_hash = _sha256(request_without_hashes)
    return {
        **request_without_hashes,
        "normalized_parameters": normalized_parameters,
        "effect_request_hash": request_hash,
        "effect_identity": "effect:" + request_hash,
    }


def _walk_keys(value: object):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


FIXTURES = _load_fixtures()
CASES = [
    pytest.param(scenario, path_name, path, id=f"{scenario['scenario_id']}:{path_name}")
    for scenario in FIXTURES["scenarios"]
    for path_name, path in FIXTURES["paths"].items()
]


@pytest.mark.parametrize(("scenario", "path_name", "path"), CASES)
def test_round2_pure_contract_chain(scenario: dict, path_name: str, path: dict):
    result = evaluate_round2_contract_chain(
        task_contract=scenario["task_contract"],
        normalized_parameters=scenario["normalized_parameters"],
        normalizer_id=scenario["normalizer"]["normalizer_id"],
        normalizer_version=scenario["normalizer"]["normalizer_version"],
        completion_requirement=FIXTURES["completion_requirement"],
        path=Round2Path.model_validate(path),
        task_id=f"task-{scenario['scenario_id']}",
        attempt_id=f"attempt-{scenario['scenario_id']}",
        execution_id=f"execution-{scenario['scenario_id']}",
    )
    assert result.requirement_evaluation.status.value == path["expected_evaluation"]
    assert result.policy_action.value == path["expected_policy_action"]
    assert result.policy_action.value in ALLOWED_POLICY_ACTIONS
    assert result.decision_application.value == path["expected_application"]
    assert (
        result.evidence_snapshot.task_contract_ref.contract_hash
        == result.task_contract.contract_hash
    )
    if result.external_operation is not None:
        assert result.external_operation.effect_identity == result.effect_identity
        assert (
            result.external_operation.effect_request_hash
            == result.effect_request_hash
        )
    if result.policy_action.value == "request_approval":
        assert result.canonical_effect_request is not None
    if result.policy_action.value == "complete":
        assert result.requirement_evaluation.status.value == "satisfied"
        assert path["operation_status"] == "confirmed"


@pytest.mark.parametrize("scenario", FIXTURES["scenarios"], ids=lambda x: x["scenario_id"])
def test_task_contract_envelope_and_effect_authorization(scenario: dict):
    contract = _materialize_contract(scenario["task_contract"])
    required = {
        "schema_version",
        "contract_id",
        "contract_version",
        "contract_hash",
        "task_type",
        "execution_type",
        "objective",
        "subject_refs",
        "input_snapshot",
        "allowed_capabilities",
        "resolved_tools",
        "constraints",
        "authorized_effects",
        "approval_requirements",
        "completion_contract_ref",
        "limits",
    }
    assert set(contract) == required
    assert not (set(_walk_keys(contract)) & FORBIDDEN_WORKFLOW_FIELDS)
    assert len(contract["authorized_effects"]) == 1
    assert len(contract["approval_requirements"]) == 1

    effect_tool_names = {
        tool["tool_name"]
        for tool in contract["resolved_tools"]
        if tool["access_mode"] == "effect"
    }
    assert effect_tool_names
    intent = contract["authorized_effects"][0]
    requirement = contract["approval_requirements"][0]
    assert intent["effect_intent_id"] in requirement["effect_intent_refs"]
    assert intent["approval_requirement_ref"] == (
        requirement["approval_requirement_id"]
        + "@"
        + requirement["approval_requirement_version"]
    )


@pytest.mark.parametrize("scenario", FIXTURES["scenarios"], ids=lambda x: x["scenario_id"])
def test_effect_identity_is_stable_and_contract_bound(scenario: dict):
    contract = _materialize_contract(scenario["task_contract"])
    first = _effect_request(scenario, contract)
    second = _effect_request(deepcopy(scenario), deepcopy(contract))
    assert first["effect_identity"] == second["effect_identity"]

    changed_contract = deepcopy(contract)
    changed_contract["contract_version"] = "2"
    changed_contract.pop("contract_hash")
    changed_contract["contract_hash"] = _sha256(changed_contract)
    changed = _effect_request(scenario, changed_contract)
    assert changed["effect_identity"] != first["effect_identity"]


def test_round2_does_not_expand_core_state_or_policy_surfaces():
    assert TASK_STATES == {
        "pending",
        "running",
        "waiting_input",
        "waiting_approval",
        "reconciling",
        "succeeded",
        "failed",
        "cancelled",
    }
    assert ALLOWED_POLICY_ACTIONS == {
        "complete",
        "continue_with_feedback",
        "start_new_attempt",
        "request_input",
        "request_approval",
        "reconcile",
        "fail",
        "escalate",
    }
    assert len(FIXTURES["scenarios"]) == 7
    assert len(FIXTURES["paths"]) == 5
