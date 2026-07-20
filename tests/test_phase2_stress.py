from __future__ import annotations

import multiprocessing
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from astra.domain import InteractionKind, ToolInvocationContext
from astra.mock_business import MockBusinessService
from astra.storage import AstraStore
from astra.tool_gateway import AstraToolGateway


TERMINATION_ATTEMPTS = 512
SIDE_EFFECT_ATTEMPTS = 512
IDEMPOTENCY_ATTEMPTS = 512
PAUSE_RACE_ATTEMPTS = 128
PROCESS_TERMINATION_COMPETITORS = 16


def _process_finalize(database: str, index: int, start_event, result_queue) -> None:
    start_event.wait(timeout=30)
    store = AstraStore(database)
    try:
        won = store.finalize_execution(
            execution_id="exec-process-race",
            status="succeeded",
            termination_reason=f"process-{index}",
        )
        result_queue.put((os.getpid(), won, None))
    except BaseException as exc:
        result_queue.put((os.getpid(), False, f"{type(exc).__name__}: {exc}"))
    finally:
        store.close()


def test_stress_exactly_once_termination_across_sqlite_connections(tmp_path):
    database = tmp_path / "termination-stress.sqlite3"
    seed = AstraStore(database)
    seed.start_execution("exec-stress", "task-stress", "attempt-stress")
    seed.close()

    def compete(index: int) -> bool:
        store = AstraStore(database)
        try:
            return store.finalize_execution(
                execution_id="exec-stress",
                status="succeeded",
                termination_reason=f"competitor-{index}",
            )
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(compete, range(TERMINATION_ATTEMPTS)))

    audit = AstraStore(database)
    try:
        assert results.count(True) == 1
        assert results.count(False) == TERMINATION_ATTEMPTS - 1
        ended = audit.connection.execute(
            """
            SELECT COUNT(*) FROM execution_events
            WHERE execution_id = 'exec-stress'
              AND event_type = 'AstraExecutionEnded'
            """
        ).fetchone()[0]
        assert ended == 1
        assert audit.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        audit.close()


def test_stress_suspension_denies_all_new_side_effects(tmp_path):
    store = AstraStore(tmp_path / "suspension-stress.sqlite3")
    store.start_execution("exec-pause", "task-pause", "attempt-pause")
    business = MockBusinessService(store)
    business.seed_normal_complaint()
    gateway = AstraToolGateway(store, business)
    store.request_interaction(
        execution_id="exec-pause",
        task_id="task-pause",
        attempt_id="attempt-pause",
        kind=InteractionKind.USER_INPUT,
        prompt="Provide required evidence",
    )

    def attempt(index: int):
        return gateway.invoke(
            ToolInvocationContext(
                execution_id="exec-pause",
                task_id="task-pause",
                attempt_id="attempt-pause",
                tool_call_id=f"pause-call-{index}",
                allowed_tools=("create_complaint_ticket",),
                idempotency_key=f"pause-key-{index}",
            ),
            "create_complaint_ticket",
            {
                "customer_id": "cust-001",
                "order_id": "order-001",
                "reason": "damaged",
                "resolution": "must not execute while suspended",
            },
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(attempt, range(SIDE_EFFECT_ATTEMPTS)))

    assert all(not result.ok for result in results)
    assert {
        result.error["type"] for result in results if result.error
    } == {"execution_suspended"}
    assert business.snapshot()["complaint_tickets"] == []
    receipts = store.connection.execute(
        "SELECT COUNT(*) FROM execution_receipts"
    ).fetchone()[0]
    assert receipts == 0


def test_stress_idempotent_side_effect_collapses_concurrent_calls(tmp_path):
    store = AstraStore(tmp_path / "idempotency-stress.sqlite3")
    store.start_execution("exec-idem", "task-idem", "attempt-idem")
    business = MockBusinessService(store)
    business.seed_normal_complaint()
    gateway = AstraToolGateway(store, business)

    def invoke(index: int):
        return gateway.invoke(
            ToolInvocationContext(
                execution_id="exec-idem",
                task_id="task-idem",
                attempt_id="attempt-idem",
                tool_call_id=f"idem-call-{index}",
                allowed_tools=("create_complaint_ticket",),
                idempotency_key="shared-idempotency-key",
            ),
            "create_complaint_ticket",
            {
                "customer_id": "cust-001",
                "order_id": "order-001",
                "reason": "damaged",
                "resolution": "single complaint despite retries",
            },
        )

    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(invoke, range(IDEMPOTENCY_ATTEMPTS)))

    assert all(result.ok for result in results)
    assert len({result.receipt_id for result in results}) == 1
    ticket_ids = {
        result.data["ticket"]["ticket_id"] for result in results
    }
    assert len(ticket_ids) == 1
    assert len(business.snapshot()["complaint_tickets"]) == 1
    receipts = store.connection.execute(
        "SELECT COUNT(*) FROM execution_receipts"
    ).fetchone()[0]
    assert receipts == 1


def test_pause_establishment_races_side_effect_with_atomic_outcomes(tmp_path):
    store = AstraStore(tmp_path / "pause-side-effect-race.sqlite3")
    business = MockBusinessService(store)
    business.seed_normal_complaint()
    gateway = AstraToolGateway(store, business)
    committed = 0
    rejected = 0

    for index in range(PAUSE_RACE_ATTEMPTS):
        execution_id = f"exec-pause-race-{index}"
        task_id = f"task-pause-race-{index}"
        attempt_id = f"attempt-pause-race-{index}"
        idempotency_key = f"pause-race-{index}"
        store.start_execution(execution_id, task_id, attempt_id)
        barrier = threading.Barrier(2)

        def request_pause():
            barrier.wait(timeout=10)
            return store.request_interaction(
                execution_id=execution_id,
                task_id=task_id,
                attempt_id=attempt_id,
                kind=InteractionKind.USER_INPUT,
                prompt="Pause concurrently with ticket creation",
            )

        def create_ticket():
            barrier.wait(timeout=10)
            return gateway.invoke(
                ToolInvocationContext(
                    execution_id=execution_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    tool_call_id=f"race-call-{index}",
                    allowed_tools=("create_complaint_ticket",),
                    idempotency_key=idempotency_key,
                ),
                "create_complaint_ticket",
                {
                    "customer_id": "cust-001",
                    "order_id": "order-001",
                    "reason": "damaged",
                    "resolution": "atomic pause race",
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            pause_future = pool.submit(request_pause)
            tool_future = pool.submit(create_ticket)
            pause_future.result(timeout=15)
            result = tool_future.result(timeout=15)

        tickets = store.query_all(
            """
            SELECT ticket_id FROM mock_complaint_tickets
            WHERE idempotency_key = ?
            """,
            (idempotency_key,),
        )
        receipts = store.query_all(
            """
            SELECT receipt_id FROM execution_receipts
            WHERE task_id = ? AND tool_name = 'create_complaint_ticket'
              AND idempotency_key = ?
            """,
            (task_id, idempotency_key),
        )
        assert store.pending_interaction(execution_id) is not None
        if result.ok:
            committed += 1
            assert len(tickets) == 1
            assert len(receipts) == 1
            assert result.data["ticket"]["ticket_id"] == tickets[0]["ticket_id"]
            assert result.receipt_id == receipts[0]["receipt_id"]
        else:
            rejected += 1
            assert result.error["type"] == "execution_suspended"
            assert tickets == []
            assert receipts == []

        subsequent = gateway.invoke(
            ToolInvocationContext(
                execution_id=execution_id,
                task_id=task_id,
                attempt_id=attempt_id,
                tool_call_id=f"post-pause-call-{index}",
                allowed_tools=("create_complaint_ticket",),
                idempotency_key=f"post-pause-{index}",
            ),
            "create_complaint_ticket",
            {
                "customer_id": "cust-001",
                "order_id": "order-001",
                "reason": "must be denied",
                "resolution": "no later side effect",
            },
        )
        assert subsequent.ok is False
        assert subsequent.error["type"] == "execution_suspended"

    assert committed + rejected == PAUSE_RACE_ATTEMPTS
    assert store.query_one("SELECT COUNT(*) FROM execution_receipts")[0] == committed
    assert store.query_one("SELECT COUNT(*) FROM mock_complaint_tickets")[0] == committed


def test_response_lost_after_commit_retries_to_same_ticket_and_receipt(tmp_path):
    store = AstraStore(tmp_path / "response-loss-idempotency.sqlite3")
    store.start_execution("exec-response-loss", "task-response-loss", "attempt-1")
    business = MockBusinessService(store)
    business.seed_normal_complaint()
    gateway = AstraToolGateway(store, business)
    arguments = {
        "customer_id": "cust-001",
        "order_id": "order-001",
        "reason": "damaged",
        "resolution": "persist before simulated response loss",
    }

    first = gateway.invoke(
        ToolInvocationContext(
            execution_id="exec-response-loss",
            task_id="task-response-loss",
            attempt_id="attempt-1",
            tool_call_id="response-lost-call",
            allowed_tools=("create_complaint_ticket",),
            idempotency_key="response-loss-key",
        ),
        "create_complaint_ticket",
        arguments,
    )
    assert first.ok is True
    # Simulate the transport dropping the response after both business state
    # and ExecutionReceipt committed. The caller retries without observing it.
    retry = gateway.invoke(
        ToolInvocationContext(
            execution_id="exec-response-loss",
            task_id="task-response-loss",
            attempt_id="attempt-1",
            tool_call_id="response-loss-retry",
            allowed_tools=("create_complaint_ticket",),
            idempotency_key="response-loss-key",
        ),
        "create_complaint_ticket",
        arguments,
    )

    assert retry.ok is True
    assert retry.data["ticket"]["ticket_id"] == first.data["ticket"]["ticket_id"]
    assert retry.receipt_id == first.receipt_id
    assert store.query_one("SELECT COUNT(*) FROM mock_complaint_tickets")[0] == 1
    assert store.query_one("SELECT COUNT(*) FROM execution_receipts")[0] == 1


def test_multiprocess_exactly_once_termination(tmp_path):
    database = tmp_path / "multiprocess-termination.sqlite3"
    seed = AstraStore(database)
    seed.start_execution(
        "exec-process-race", "task-process-race", "attempt-process-race"
    )
    seed.close()

    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_process_finalize,
            args=(str(database), index, start_event, result_queue),
        )
        for index in range(PROCESS_TERMINATION_COMPETITORS)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=45) for _ in processes]
    for process in processes:
        process.join(timeout=45)
        assert process.exitcode == 0

    process_ids = {pid for pid, _, _ in results}
    errors = [error for _, _, error in results if error]
    winners = [won for _, won, _ in results]
    assert len(process_ids) == PROCESS_TERMINATION_COMPETITORS
    assert errors == []
    assert winners.count(True) == 1

    audit = AstraStore(database)
    try:
        assert audit.query_one(
            """
            SELECT COUNT(*) FROM execution_events
            WHERE execution_id = 'exec-process-race'
              AND event_type = 'AstraExecutionEnded'
            """
        )[0] == 1
        assert audit.query_one("PRAGMA integrity_check")[0] == "ok"
    finally:
        audit.close()
