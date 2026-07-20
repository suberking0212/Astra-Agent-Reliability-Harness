from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from astra.budget import BudgetLedger
from astra.domain import RuntimeInvocation
from astra.hermes_adapter import HermesExecutor
from astra.mock_business import MockBusinessService
from astra.result_validator import MinimalResultValidator
from astra.storage import AstraStore
from astra.tool_gateway import AstraToolGateway
from astra.trace import NeutralTraceCollector


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = WORKSPACE_ROOT / "hermes-agent-main"


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
                        "idempotency_key": "task-phase2-ticket",
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


def test_phase2_normal_complaint_vertical_slice(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
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

    store = AstraStore(tmp_path / "phase2.sqlite3")
    business = MockBusinessService(store)
    business.seed_normal_complaint()
    gateway = AstraToolGateway(store, business)
    validator = MinimalResultValidator(store, business)
    budget = BudgetLedger(store)
    trace = NeutralTraceCollector(store)
    executor = HermesExecutor(
        hermes_root=HERMES_ROOT,
        store=store,
        gateway=gateway,
        validator=validator,
        budget=budget,
        trace=trace,
        client_factory=lambda _: _client(),
    )
    invocation = RuntimeInvocation(
        execution_id="exec-phase2-normal",
        task_id="task-phase2-normal",
        attempt_id="attempt-phase2-normal",
        user_request=(
            "The customer received a damaged item after the normal return window. "
            "Investigate and handle the complaint using the available tools."
        ),
        task_contract={
            "task_type": "complaint_resolution",
            "execution_type": "tool_execution",
            "allowed_capabilities": ["complaint_investigation", "ticket_creation"],
            "completion_requirements": [
                "complaint resolution outcome exists",
                "required business state is persisted",
                "required side effects are verified",
            ],
        },
        allowed_tools=(
            "get_order",
            "search_policy",
            "create_complaint_ticket",
        ),
        provider_config={
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "phase2-dummy-key",
            "provider": "custom",
            "api_mode": "chat_completions",
            "model": "astra-phase2-scripted-provider",
        },
        limits={"max_agent_steps": 8, "budget_mode": "observe_only"},
    )
    observed_events = []

    async def sink(event):
        observed_events.append(event)

    result = asyncio.run(executor.execute(invocation, sink))

    assert result.status.value == "succeeded"
    assert result.task_outcome_validated is True
    assert result.agent_turn_finished is True
    assert result.result_receipt["valid"] is True
    assert result.usage.total_tokens == 200
    assert len(business.snapshot()["complaint_tickets"]) == 1
    assert store.finalize_execution(
        execution_id=invocation.execution_id,
        status="failed",
        termination_reason="duplicate",
    ) is False

    event_types = [event.event_type for event in observed_events]
    assert event_types[0] == "ExecutionStarted"
    assert "ResultSubmission" in event_types
    assert "ResultValidation" in event_types
    assert event_types[-1] == "ExecutionEnded"
    trace_types = [row["event_type"] for row in trace.timeline(invocation.execution_id)]
    assert "ToolCall" in trace_types
    assert "ProviderCall" in trace_types
    assert "ExecutionEnded" in trace_types


def _client():
    client = MagicMock()
    client.chat.completions.create.side_effect = _scripted_provider
    return client
