from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from astra.budget import BudgetLedger
from astra.domain import InteractionKind, RuntimeInvocation, ToolInvocationContext
from astra.hermes_adapter.context import ExecutionBridgeContext
from astra.mock_business import MockBusinessService
from astra.result_validator import MinimalResultValidator
from astra.runtime import Phase2Runtime
from astra.storage import AstraStore
from astra.tool_gateway import AstraToolGateway
from astra.trace import NeutralTraceCollector


def _invocation(execution_id: str = "exec-1") -> RuntimeInvocation:
    return RuntimeInvocation(
        execution_id=execution_id,
        task_id="task-1",
        attempt_id="attempt-1",
        user_request="Resolve the damaged order complaint.",
        task_contract={
            "task_type": "complaint_resolution",
            "execution_type": "tool_execution",
            "completion_requirements": ["complaint_ticket_exists"],
        },
        allowed_tools=(
            "get_customer",
            "get_order",
            "search_policy",
            "create_complaint_ticket",
            "get_complaint_ticket",
        ),
        provider_config={"provider": "custom", "model": "scripted"},
    )


def _setup(tmp_path):
    store = AstraStore(tmp_path / "astra.sqlite3")
    store.start_execution("exec-1", "task-1", "attempt-1")
    business = MockBusinessService(store)
    business.seed_normal_complaint()
    gateway = AstraToolGateway(store, business)
    return store, business, gateway


def _context(
    *,
    tool_call_id: str,
    idempotency_key: str | None = None,
    allowed_tools: tuple[str, ...] | None = None,
) -> ToolInvocationContext:
    return ToolInvocationContext(
        execution_id="exec-1",
        task_id="task-1",
        attempt_id="attempt-1",
        tool_call_id=tool_call_id,
        allowed_tools=allowed_tools or _invocation().allowed_tools,
        idempotency_key=idempotency_key,
    )


def test_tool_gateway_validates_permissions_schema_and_idempotency(tmp_path):
    store, business, gateway = _setup(tmp_path)

    denied = gateway.invoke(
        _context(tool_call_id="call-denied", allowed_tools=("get_order",)),
        "create_complaint_ticket",
        {
            "customer_id": "cust-001",
            "order_id": "order-001",
            "reason": "damaged",
            "resolution": "complaint review",
        },
    )
    assert denied.ok is False
    assert denied.error["type"] == "permission_denied"

    invalid = gateway.invoke(
        _context(tool_call_id="call-invalid"),
        "get_order",
        {"wrong": "value"},
    )
    assert invalid.ok is False
    assert invalid.error["type"] == "invalid_tool_arguments"

    arguments = {
        "customer_id": "cust-001",
        "order_id": "order-001",
        "reason": "damaged in transit",
        "resolution": "open a complaint investigation",
    }
    first = gateway.invoke(
        _context(tool_call_id="call-create-1", idempotency_key="complaint-001"),
        "create_complaint_ticket",
        arguments,
    )
    second = gateway.invoke(
        _context(tool_call_id="call-create-2", idempotency_key="complaint-001"),
        "create_complaint_ticket",
        arguments,
    )
    assert first.ok is True
    assert second.ok is True
    assert first.data["ticket"]["ticket_id"] == second.data["ticket"]["ticket_id"]
    assert len(business.snapshot()["complaint_tickets"]) == 1


def test_suspension_blocks_new_side_effects(tmp_path):
    store, _, gateway = _setup(tmp_path)
    store.request_interaction(
        execution_id="exec-1",
        task_id="task-1",
        attempt_id="attempt-1",
        kind=InteractionKind.USER_INPUT,
        prompt="Provide a damage photo.",
    )
    result = gateway.invoke(
        _context(tool_call_id="call-after-pause", idempotency_key="paused"),
        "create_complaint_ticket",
        {
            "customer_id": "cust-001",
            "order_id": "order-001",
            "reason": "damaged",
            "resolution": "review",
        },
    )
    assert result.ok is False
    assert result.error["type"] == "execution_suspended"


def test_result_validation_uses_receipts_and_actual_business_state(tmp_path):
    store, business, gateway = _setup(tmp_path)
    invocation = _invocation()
    created = gateway.invoke(
        _context(tool_call_id="call-create", idempotency_key="result-validation"),
        "create_complaint_ticket",
        {
            "customer_id": "cust-001",
            "order_id": "order-001",
            "reason": "damaged",
            "resolution": "open complaint",
        },
    )
    validator = MinimalResultValidator(store, business)
    ticket_id = created.data["ticket"]["ticket_id"]
    valid = validator.validate(
        invocation,
        {
            "customer_id": "cust-001",
            "order_id": "order-001",
            "ticket_id": ticket_id,
            "resolution": "complaint_created",
        },
        [created.receipt_id],
        [created.receipt_id],
    )
    assert valid.valid is True

    false_completion = validator.validate(
        invocation,
        {
            "customer_id": "cust-001",
            "order_id": "order-001",
            "ticket_id": "ticket-does-not-exist",
        },
        [created.receipt_id],
        [created.receipt_id],
    )
    assert false_completion.valid is False
    assert "complaint_ticket_exists" in false_completion.errors


def test_exactly_once_execution_termination_is_database_backed(tmp_path):
    store = AstraStore(tmp_path / "termination.sqlite3")
    store.start_execution("exec-race", "task-race", "attempt-race")

    def finish(index: int) -> bool:
        return store.finalize_execution(
            execution_id="exec-race",
            status="succeeded",
            termination_reason=f"competitor-{index}",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = list(pool.map(finish, range(24)))

    assert winners.count(True) == 1
    ended_events = store.connection.execute(
        """
        SELECT COUNT(*) FROM execution_events
        WHERE execution_id = 'exec-race'
          AND event_type = 'AstraExecutionEnded'
        """
    ).fetchone()[0]
    assert ended_events == 1


def test_budget_ledger_observe_and_conservative_limit(tmp_path):
    store = AstraStore(tmp_path / "budget.sqlite3")
    store.start_execution("exec-budget", "task-budget", "attempt-budget")
    ledger = BudgetLedger(store)
    ledger.observe_provider_call(
        "exec-budget",
        {"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
    )
    ledger.reserve_next_call("exec-budget", 70)

    assert ledger.decide(
        "exec-budget", {"budget_mode": "observe_only", "token_budget": 50}
    ).allowed
    decision = ledger.decide(
        "exec-budget", {"budget_mode": "conservative_limit", "token_budget": 100}
    )
    assert decision.allowed is False
    assert decision.reason == "token_budget_conservative_limit"
    assert ledger.usage("exec-budget").total_tokens == 40


def test_interaction_persists_suspends_and_resumes_as_new_turn(tmp_path):
    store, business, gateway = _setup(tmp_path)
    invocation = _invocation()
    context = ExecutionBridgeContext(
        invocation=invocation,
        store=store,
        gateway=gateway,
        validator=MinimalResultValidator(store, business),
        budget=BudgetLedger(store),
        trace=NeutralTraceCollector(store),
    )
    interrupt_reasons = []
    context.bind_interrupt(interrupt_reasons.append)

    response = context.request_interaction(
        InteractionKind.USER_INPUT,
        "Please provide a damage photo reference.",
        {"required_field": "damage_photo_ref"},
    )
    assert '"suspension_requested": true' in response
    assert store.is_suspended(invocation.execution_id) is True
    assert interrupt_reasons == ["astra_waiting_user_input"]
    pending = store.pending_interaction(invocation.execution_id)
    assert pending is not None

    store.save_session_handle(invocation.execution_id, "opaque-session-handle")
    resumed = Phase2Runtime(store).resolve_and_resume(
        invocation,
        interaction_id=pending["interaction_id"],
        resolution={"damage_photo_ref": "evidence-photo-001"},
        new_execution_id="exec-2",
    )
    assert resumed.execution_id == "exec-2"
    assert resumed.session_handle == "opaque-session-handle"
    assert resumed.feedback[-1]["type"] == "InteractionResolution"
    assert store.is_suspended(invocation.execution_id) is False
