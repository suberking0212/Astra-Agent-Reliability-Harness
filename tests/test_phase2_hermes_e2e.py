from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from astra.phase3.completion import (
    CompletionContract,
    CompletionRequirement,
    EvaluatorRef,
)
from astra.phase3.task_contract import TaskContract
from astra.production import ProductionConfig, ProductionRuntime
from astra.tool_gateway import production_tool_schema_hash


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = WORKSPACE_ROOT / "hermes-agent-main"


def _single_worker_contracts() -> tuple[TaskContract, CompletionContract]:
    task_contract = TaskContract.materialize(
        {
            "schema_version": "1",
            "contract_id": "contract-hermes-single-worker-integration",
            "contract_version": "1",
            "task_type": "hermes_single_worker_integration",
            "execution_type": "tool_execution",
            "objective": {
                "description": (
                    "Investigate the damaged order and create a complaint ticket."
                )
            },
            "subject_refs": [
                {
                    "authority_domain": "commerce.mock",
                    "type": "order",
                    "id": "order-001",
                }
            ],
            "input_snapshot": {
                "schema_id": "test.hermes_single_worker",
                "schema_version": "1",
                "values": {"order_id": "order-001"},
                "content_hash": "sha256:test-hermes-single-worker-input",
            },
            "allowed_capabilities": [
                {
                    "capability_id": "complaints.integration",
                    "capability_version": "1",
                }
            ],
            "resolved_tools": [
                {
                    "tool_name": "get_order",
                    "tool_version": "1",
                    "schema_hash": production_tool_schema_hash("get_order"),
                    "capability_ref": "complaints.integration@1",
                    "access_mode": "read",
                },
                {
                    "tool_name": "search_policy",
                    "tool_version": "1",
                    "schema_hash": production_tool_schema_hash("search_policy"),
                    "capability_ref": "complaints.integration@1",
                    "access_mode": "read",
                },
                {
                    "tool_name": "create_complaint_ticket",
                    "tool_version": "1",
                    "schema_hash": production_tool_schema_hash(
                        "create_complaint_ticket"
                    ),
                    "capability_ref": "complaints.integration@1",
                    "access_mode": "effect",
                },
            ],
            "constraints": [],
            "authorized_effects": [
                {
                    "effect_intent_id": "create-integration-complaint",
                    "effect_type": "support.complaint_ticket",
                    "effect_type_version": "1",
                    "authority_domain": "support.mock",
                    "subject_ref": {
                        "authority_domain": "commerce.mock",
                        "type": "order",
                        "id": "order-001",
                    },
                    "parameter_constraints": {},
                    "max_confirmed_occurrences": 1,
                }
            ],
            "approval_requirements": [],
            "completion_contract_ref": {
                "contract_id": "completion-hermes-single-worker-integration",
                "contract_version": "1",
            },
            "limits": {
                "max_attempts": 1,
                "task_deadline": "2099-01-01T00:00:00Z",
                "max_executions_per_attempt": 1,
                "max_feedback_cycles": 0,
                "max_reconcile_cycles": 0,
            },
        }
    )
    completion_contract = CompletionContract(
        contract_id=task_contract.completion_contract_ref.contract_id,
        contract_version=task_contract.completion_contract_ref.contract_version,
        task_type=task_contract.task_type,
        requirements=(
            CompletionRequirement(
                requirement_id="confirmed_complaint_effect",
                description=(
                    "The complaint effect is confirmed by authoritative state."
                ),
                evaluator=EvaluatorRef(
                    evaluator_id="astra.authorized_effect_confirmed",
                    evaluator_version="1",
                ),
                configuration={
                    "effect_intent_ref": "create-integration-complaint"
                },
                required_evidence=("external_operation", "business_state"),
            ),
        ),
    )
    return task_contract, completion_contract


def _tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def _response(*, content="", finish_reason="stop", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=30,
        completion_tokens=10,
        total_tokens=40,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    return SimpleNamespace(
        id="phase2-scripted-response",
        choices=[choice],
        model="astra-phase2-scripted-provider",
        usage=usage,
    )


def _parse_tool_messages(messages):
    parsed = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        parsed.append(json.loads(str(message.get("content", "{}"))))
    return parsed


def _scripted_provider(**request):
    tool_names = {
        item["function"]["name"] for item in request.get("tools") or []
    }
    assert {
        "get_order",
        "search_policy",
        "create_complaint_ticket",
        "submit_task_result",
    }.issubset(tool_names)
    assert "get_customer" not in tool_names
    assert "get_complaint_ticket" not in tool_names
    assert "ASTRA_TASK_CONTRACT=" in request["messages"][0]["content"]

    tool_results = _parse_tool_messages(request["messages"])
    if len(tool_results) == 0:
        return _response(
            finish_reason="tool_calls",
            tool_calls=[
                _tool_call("call-order", "get_order", {"order_id": "order-001"})
            ],
        )
    if len(tool_results) == 1:
        return _response(
            finish_reason="tool_calls",
            tool_calls=[
                _tool_call(
                    "call-policy",
                    "search_policy",
                    {"query": "damaged goods return window exception"},
                )
            ],
        )
    if len(tool_results) == 2:
        return _response(
            finish_reason="tool_calls",
            tool_calls=[
                _tool_call(
                    "call-ticket",
                    "create_complaint_ticket",
                    {
                        "customer_id": "cust-001",
                        "order_id": "order-001",
                        "reason": "item damaged in transit",
                        "resolution": "create complaint for policy exception review",
                    },
                )
            ],
        )
    if len(tool_results) == 3:
        receipt_refs = [result["receipt_id"] for result in tool_results]
        ticket_id = tool_results[-1]["data"]["ticket"]["ticket_id"]
        return _response(
            finish_reason="tool_calls",
            tool_calls=[
                _tool_call(
                    "call-submit",
                    "submit_task_result",
                    {
                        "outcome": {
                            "customer_id": "cust-001",
                            "order_id": "order-001",
                            "ticket_id": ticket_id,
                            "resolution": "complaint_created",
                        },
                        "evidence_refs": receipt_refs,
                        "receipt_refs": receipt_refs,
                    },
                )
            ],
        )
    assert tool_results[-1]["valid"] is True
    return _response(
        content="Complaint ticket created and validated against business state.",
        finish_reason="stop",
    )


def _client():
    client = MagicMock()
    client.chat.completions.create.side_effect = _scripted_provider
    return client


def test_single_worker_with_real_hermes_executor_finalizes_once(
    tmp_path,
    monkeypatch,
):
    hermes_home = tmp_path / "hermes-worker-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - astra_bridge\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
    monkeypatch.chdir(WORKSPACE_ROOT)

    sys.path.insert(0, str(HERMES_ROOT))
    try:
        import hermes_cli.plugins as plugins

        plugins._plugin_manager = plugins.PluginManager()
    finally:
        sys.path.remove(str(HERMES_ROOT))

    database = tmp_path / "single-worker-hermes.sqlite3"
    task_contract, completion_contract = _single_worker_contracts()
    config = ProductionConfig(
        database_path=database,
        hermes_root=HERMES_ROOT,
        provider_config={
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "phase2-dummy-key",
            "provider": "custom",
            "api_mode": "chat_completions",
            "model": "astra-phase2-scripted-provider",
        },
        hermes_session_database_path=database.with_suffix(".hermes.sqlite3"),
    )
    with ProductionRuntime(
        config, hermes_client_factory=lambda _: _client()
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit-real-hermes-worker",
            task_id="task-real-hermes-worker",
            contract=task_contract,
            completion_contract=completion_contract,
            ready_at="2026-07-20T00:00:00+00:00",
        )
        result = asyncio.run(
            app.run_worker_once()
        )

        assert result is not None
        assert result.execution_finalized is True
        assert result.run_request_state == "completed"
        execution = app.store.get_execution(result.claim.execution_id)
        assert execution is not None
        assert execution["ended_at"] is not None
        assert app.store.query_one(
            """
            SELECT COUNT(*) FROM execution_events
            WHERE execution_id = ? AND event_type = 'AstraExecutionEnded'
            """,
            (result.claim.execution_id,),
        )[0] == 1
        task = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        assert task["state"] == "succeeded"
