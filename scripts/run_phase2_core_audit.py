#!/usr/bin/env python3
"""Run the deterministic Hermes Phase 2 slice and export persistence evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


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
        id="phase2-baseline-response",
        choices=[choice],
        model="astra-phase2-baseline-provider",
        usage=usage,
    )


class ScriptedComplaintProvider:
    def __init__(self) -> None:
        self.seen_tool_sets: list[list[str]] = []

    def __call__(self, **request):
        tool_names = sorted(
            item["function"]["name"] for item in request.get("tools") or []
        )
        self.seen_tool_sets.append(tool_names)
        tool_results = [
            json.loads(str(message.get("content", "{}")))
            for message in request["messages"]
            if message.get("role") == "tool"
        ]
        if not tool_results:
            return _response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call("audit-order", "get_order", {"order_id": "order-001"})
                ],
            )
        if len(tool_results) == 1:
            return _response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "audit-policy",
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
                        "audit-ticket",
                        "create_complaint_ticket",
                        {
                            "customer_id": "cust-001",
                            "order_id": "order-001",
                            "reason": "item damaged in transit",
                            "resolution": "create complaint for exception review",
                            "idempotency_key": "phase2-core-baseline-ticket",
                        },
                    )
                ],
            )
        if len(tool_results) == 3:
            receipt_refs = [item["receipt_id"] for item in tool_results]
            ticket_id = tool_results[-1]["data"]["ticket"]["ticket_id"]
            return _response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "audit-submit",
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
        if not tool_results[-1].get("valid"):
            raise AssertionError("Baseline result validation was not valid")
        return _response(
            content="Complaint ticket created and validated against business state."
        )


def _write_json(path: Path, value) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=WORKSPACE_ROOT / "artifacts" / "phase2-core-runtime-audit",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    hermes_home = output_dir / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - astra_bridge\n",
        encoding="utf-8",
    )
    os.environ["HERMES_HOME"] = str(hermes_home)
    os.environ["HERMES_ENABLE_PROJECT_PLUGINS"] = "1"
    os.chdir(WORKSPACE_ROOT)

    sys.path.insert(0, str(WORKSPACE_ROOT))
    sys.path.insert(0, str(HERMES_ROOT))
    try:
        import hermes_cli.plugins as plugins

        plugins._plugin_manager = plugins.PluginManager()

        from astra.budget import BudgetLedger
        from astra.domain import RuntimeInvocation
        from astra.hermes_adapter import HermesExecutor
        from astra.mock_business import MockBusinessService
        from astra.result_validator import MinimalResultValidator
        from astra.storage import AstraStore
        from astra.tool_gateway import AstraToolGateway
        from astra.trace import NeutralTraceCollector

        database = output_dir / "phase2-core.sqlite3"
        store = AstraStore(database)
        business = MockBusinessService(store)
        business.seed_normal_complaint()
        gateway = AstraToolGateway(store, business)
        budget = BudgetLedger(store)
        trace = NeutralTraceCollector(store)
        scripted = ScriptedComplaintProvider()

        def client_factory(_):
            client = MagicMock()
            client.chat.completions.create.side_effect = scripted
            return client

        executor = HermesExecutor(
            hermes_root=HERMES_ROOT,
            store=store,
            gateway=gateway,
            validator=MinimalResultValidator(store, business),
            budget=budget,
            trace=trace,
            client_factory=client_factory,
        )
        invocation = RuntimeInvocation(
            execution_id="exec-phase2-core-baseline",
            task_id="task-phase2-core-baseline",
            attempt_id="attempt-phase2-core-baseline",
            user_request=(
                "Customer cust-001 received order-001 damaged after the normal "
                "return window. Investigate policy, create the complaint ticket, "
                "and submit a receipt-backed result."
            ),
            task_contract={
                "task_type": "complaint_resolution",
                "execution_type": "tool_execution",
                "completion_requirements": [
                    "complaint ticket exists",
                    "business state is persisted",
                    "receipt evidence is valid",
                ],
            },
            allowed_tools=(
                "get_order",
                "search_policy",
                "create_complaint_ticket",
            ),
            provider_config={
                "base_url": "http://127.0.0.1:9/v1",
                "api_key": "baseline-dummy-key",
                "provider": "custom",
                "api_mode": "chat_completions",
                "model": "astra-phase2-baseline-provider",
            },
            limits={"max_agent_steps": 8, "budget_mode": "observe_only"},
        )
        observed_events = []

        async def sink(event):
            observed_events.append(event.model_dump(mode="json"))

        result = asyncio.run(executor.execute(invocation, sink))
        trace_rows = trace.timeline(invocation.execution_id)
        receipt_rows = [
            dict(row)
            for row in store.query_all(
                "SELECT * FROM execution_receipts ORDER BY created_at"
            )
        ]
        for row in receipt_rows:
            row["request"] = json.loads(row.pop("request_json"))
            row["result"] = json.loads(row.pop("result_json"))
        result_receipts = [
            json.loads(row["receipt_json"])
            for row in store.query_all(
                "SELECT receipt_json FROM result_receipts ORDER BY created_at"
            )
        ]
        execution = dict(store.get_execution(invocation.execution_id) or {})
        business_state = business.snapshot()
        budget_snapshot = budget.snapshot(invocation.execution_id)
        trace_types = [row["event_type"] for row in trace_rows]
        hermes_db = sqlite3.connect(hermes_home / "state.db")
        hermes_db.row_factory = sqlite3.Row
        hermes_messages = [
            dict(row)
            for row in hermes_db.execute(
                """
                SELECT session_id, role, tool_call_id, tool_name, finish_reason
                FROM messages ORDER BY id
                """
            )
        ]
        hermes_integrity = hermes_db.execute("PRAGMA integrity_check").fetchone()[0]
        hermes_sessions = hermes_db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        hermes_db.close()
        hermes_roles = [row["role"] for row in hermes_messages]
        expected_tools = {
            "get_order",
            "search_policy",
            "create_complaint_ticket",
            "request_user_input",
            "request_approval",
            "submit_task_result",
        }
        forbidden_tools = {"get_customer", "get_complaint_ticket"}
        checks = {
            "sqlite_integrity": store.query_one("PRAGMA integrity_check")[0] == "ok",
            "foreign_keys_clean": not store.query_all("PRAGMA foreign_key_check"),
            "execution_succeeded": execution.get("status") == "succeeded",
            "execution_ended_once": store.query_one(
                """
                SELECT COUNT(*) FROM execution_events
                WHERE execution_id = ? AND event_type = 'AstraExecutionEnded'
                """,
                (invocation.execution_id,),
            )[0]
            == 1,
            "result_validated": bool(result.task_outcome_validated),
            "result_receipt_valid": bool(
                result_receipts and result_receipts[-1].get("valid")
            ),
            "three_business_receipts": len(receipt_rows) == 3,
            "one_ticket_persisted": len(business_state["complaint_tickets"]) == 1,
            "trace_has_start_and_end": bool(
                trace_types
                and trace_types[0] == "ExecutionStarted"
                and trace_types[-1] == "ExecutionEnded"
            ),
            "trace_has_submission_before_validation": (
                trace_types.index("ResultSubmission")
                < trace_types.index("ResultValidation")
            ),
            "budget_observed_five_calls": budget_snapshot[
                "provider_request_count"
            ]
            == 5,
            "budget_observed_200_tokens": budget_snapshot[
                "observed_total_tokens"
            ]
            == 200,
            "tool_visibility_exact": all(
                set(names) == expected_tools for names in scripted.seen_tool_sets
            ),
            "forbidden_tools_absent": all(
                forbidden_tools.isdisjoint(names) for names in scripted.seen_tool_sets
            ),
            "hermes_session_integrity": hermes_integrity == "ok",
            "hermes_one_session_persisted": hermes_sessions == 1,
            "hermes_ten_messages_persisted": len(hermes_messages) == 10,
            "hermes_role_sequence_valid": hermes_roles
            == [
                "user",
                "assistant",
                "tool",
                "assistant",
                "tool",
                "assistant",
                "tool",
                "assistant",
                "tool",
                "assistant",
            ],
        }
        audit = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "execution_id": invocation.execution_id,
            "checks": checks,
            "all_checks_passed": all(checks.values()),
            "counts": {
                "trace_spans": len(trace_rows),
                "execution_receipts": len(receipt_rows),
                "result_receipts": len(result_receipts),
                "business_tickets": len(business_state["complaint_tickets"]),
                "provider_calls": budget_snapshot["provider_request_count"],
                "hermes_sessions": hermes_sessions,
                "hermes_messages": len(hermes_messages),
            },
            "trace_event_types": trace_types,
            "receipt_tool_names": [row["tool_name"] for row in receipt_rows],
            "observed_tool_sets": scripted.seen_tool_sets,
        }
        _write_json(output_dir / "execution-result.json", result.model_dump(mode="json"))
        _write_json(output_dir / "execution.json", execution)
        _write_json(output_dir / "execution-receipts.json", receipt_rows)
        _write_json(output_dir / "result-receipts.json", result_receipts)
        _write_json(output_dir / "business-state.json", business_state)
        _write_json(output_dir / "budget-ledger.json", budget_snapshot)
        _write_json(output_dir / "observer-events.json", observed_events)
        _write_json(
            output_dir / "hermes-session-audit.json",
            {
                "integrity_check": hermes_integrity,
                "session_count": hermes_sessions,
                "message_count": len(hermes_messages),
                "roles": hermes_roles,
                "messages": hermes_messages,
            },
        )
        _write_json(output_dir / "audit-report.json", audit)
        with (output_dir / "neutral-trace.jsonl").open("w", encoding="utf-8") as handle:
            for row in trace_rows:
                handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
        store.close()
        print(json.dumps(audit, ensure_ascii=False, indent=2))
        return 0 if audit["all_checks_passed"] else 1
    finally:
        for path in (str(HERMES_ROOT), str(WORKSPACE_ROOT)):
            if path in sys.path:
                sys.path.remove(path)


if __name__ == "__main__":
    raise SystemExit(main())
