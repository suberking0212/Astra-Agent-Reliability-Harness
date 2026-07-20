from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest


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


def _approval_matches(contract: dict, effect: dict, approval_status: str) -> bool:
    if approval_status != "approved":
        return False
    requirement = contract["approval_requirements"][0]
    return (
        effect["effect_intent_ref"] in requirement["effect_intent_refs"]
        and requirement["usage_semantics"] == "single_effect_single_use"
        and effect["task_contract_ref"]["contract_hash"] == contract["contract_hash"]
    )


def _evaluate(path: dict, approval_matches: bool) -> str:
    if not path["input_complete"]:
        return "unknown"
    if not approval_matches:
        return "unknown"
    if path["operation_status"] == "confirmed":
        return "satisfied"
    if path["operation_status"] in {"not_started", "indeterminate"}:
        return "unknown"
    return "unsatisfied"


def _policy_action(path: dict, evaluation: str, approval_matches: bool) -> str:
    if not path["input_complete"]:
        return "request_input"
    if not approval_matches:
        return "request_approval"
    if path["operation_status"] == "indeterminate":
        return "reconcile"
    if evaluation == "satisfied":
        return "complete"
    return "continue_with_feedback"


def _apply_decision(expected_task_version: int, current_task_version: int) -> str:
    return "applied" if expected_task_version == current_task_version else "stale"


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
    contract = _materialize_contract(scenario["task_contract"])
    effect = _effect_request(scenario, contract) if path["input_complete"] else None
    approval_matches = bool(
        effect and _approval_matches(contract, effect, path["approval_status"])
    )

    external_operation = None
    if effect and approval_matches and path["operation_status"] != "not_started":
        external_operation = {
            "effect_identity": effect["effect_identity"],
            "effect_request_hash": effect["effect_request_hash"],
            "idempotency_key": _sha256(
                {
                    "effect_identity": effect["effect_identity"],
                    "adapter_key_version": "1",
                }
            ),
            "status": path["operation_status"],
        }

    evidence_snapshot = {
        "task_contract_ref": {
            "contract_id": contract["contract_id"],
            "contract_version": contract["contract_version"],
            "contract_hash": contract["contract_hash"],
        },
        "effect_identity": effect["effect_identity"] if effect else None,
        "effect_request_hash": effect["effect_request_hash"] if effect else None,
        "approval_status": path["approval_status"],
        "external_operation": external_operation,
    }
    evaluation = _evaluate(path, approval_matches)
    action = _policy_action(path, evaluation, approval_matches)
    expected_task_version = 12
    current_task_version = expected_task_version + path["current_task_version_offset"]
    application = _apply_decision(expected_task_version, current_task_version)

    assert evaluation == path["expected_evaluation"], path_name
    assert action == path["expected_policy_action"], path_name
    assert action in ALLOWED_POLICY_ACTIONS
    assert application == path["expected_application"], path_name
    assert evidence_snapshot["task_contract_ref"]["contract_hash"] == contract["contract_hash"]

    if external_operation:
        assert external_operation["effect_identity"] == effect["effect_identity"]
        assert external_operation["effect_request_hash"] == effect["effect_request_hash"]
    if action == "request_approval":
        assert effect is not None
        assert effect["effect_identity"].startswith("effect:sha256:")
    if action == "complete":
        assert evaluation == "satisfied"
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
