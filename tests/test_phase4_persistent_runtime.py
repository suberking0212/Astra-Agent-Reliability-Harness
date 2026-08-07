"""Storage/transaction component tests; not Production Batch acceptance.

Constructed claims, lease tokens, and ``ExecutionResult`` values exercise
persistence and fencing APIs only.  They do not prove real Worker ownership,
Receipt production, Completion evaluation, or process recovery.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

from astra.domain import ExecutionResult, ExecutionStatus
from astra.phase3.completion import (
    CompletionContract,
    CompletionRequirement,
    EvaluatorRef,
)
from astra.phase3.task_contract import TaskContract
from astra.runtime import (
    CommandIdentityConflict,
    ExecutionEligibilityError,
    ExecutionResultConflict,
    TaskRuntime,
)
from astra.storage import CURRENT_SCHEMA_VERSION, AstraStore
from astra.tool_catalog import allowed_capabilities, resolved_tools


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "phase3_contract_round2.json"
CLAIM_TIME = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
READY_AT = "2026-07-20T00:00:00+00:00"
LEASE_OWNER_ID = "test-phase4-worker"
LEASE_DURATION_SECONDS = 30.0


def _contract() -> TaskContract:
    fixture = json.loads(FIXTURE_PATH.read_text())
    payload = fixture["scenarios"][0]["task_contract"]
    payload["allowed_capabilities"] = [
        item.model_dump(mode="json")
        for item in allowed_capabilities(("create_complaint_ticket",))
    ]
    payload["resolved_tools"] = [
        item.model_dump(mode="json")
        for item in resolved_tools(("create_complaint_ticket",))
    ]
    return TaskContract.materialize(payload)


def _deadline_contract(
    *,
    contract_id: str,
    task_deadline: str,
    attempt_deadline: str | None,
) -> TaskContract:
    limits = {
        "max_attempts": 2,
        "task_deadline": task_deadline,
        "max_executions_per_attempt": 4,
        "max_feedback_cycles": 1,
        "max_reconcile_cycles": 1,
    }
    if attempt_deadline is not None:
        limits["attempt_deadline"] = attempt_deadline
    return _independent_contract(
        contract_id=contract_id,
        task_type="deadline_boundary",
        completion_contract_id="completion-deadline-boundary",
        limits=limits,
    )


def _independent_contract(
    *,
    contract_id: str,
    task_type: str,
    completion_contract_id: str,
    limits: dict,
) -> TaskContract:
    return TaskContract.materialize(
        {
            "schema_version": "1",
            "contract_id": contract_id,
            "contract_version": "1",
            "task_type": task_type,
            "execution_type": "tool_execution",
            "objective": {
                "description": "Evaluate the submitted result under its contract."
            },
            "subject_refs": [],
            "input_snapshot": {
                "schema_id": "runtime.audit.input",
                "schema_version": "1",
                "values": {},
                "content_hash": "sha256:runtime-audit-input",
            },
            "allowed_capabilities": [],
            "resolved_tools": [],
            "constraints": [],
            "authorized_effects": [],
            "approval_requirements": [],
            "completion_contract_ref": {
                "contract_id": completion_contract_id,
                "contract_version": "1",
            },
            "limits": limits,
        }
    )


def _registry_test_contract() -> TaskContract:
    return _independent_contract(
        contract_id="contract-configured-runtime-test",
        task_type="configured_runtime_test",
        completion_contract_id="completion-configured-runtime-test",
        limits={
            "max_attempts": 1,
            "task_deadline": "2026-07-21T00:00:00Z",
            "max_executions_per_attempt": 1,
            "max_feedback_cycles": 1,
            "max_reconcile_cycles": 1,
        },
    )


def _unregistered_completion_contract() -> CompletionContract:
    task_contract = _registry_test_contract()
    return CompletionContract(
        contract_id=task_contract.completion_contract_ref.contract_id,
        contract_version=task_contract.completion_contract_ref.contract_version,
        task_type=task_contract.task_type,
        requirements=(
            CompletionRequirement(
                requirement_id="unregistered_registry_probe",
                description="This requirement intentionally has no registered evaluator.",
                evaluator=EvaluatorRef(
                    evaluator_id="audit.unregistered_evaluator",
                    evaluator_version="1",
                ),
            ),
        ),
    )


def _frozen_complaint_completion_contract() -> CompletionContract:
    fixture = json.loads(FIXTURE_PATH.read_text())
    task_contract = _contract()
    return CompletionContract(
        contract_id=task_contract.completion_contract_ref.contract_id,
        contract_version=task_contract.completion_contract_ref.contract_version,
        task_type=task_contract.task_type,
        requirements=(
            CompletionRequirement.model_validate(
                fixture["completion_requirement"]
            ),
        ),
    )


def _count(store: AstraStore, table: str) -> int:
    return int(store.query_one(f"SELECT COUNT(*) FROM {table}")[0])


def test_new_database_initializes_target_schema_version(tmp_path):
    store = AstraStore(tmp_path / "new.sqlite3")
    try:
        assert store.schema_version == CURRENT_SCHEMA_VERSION
        tables = {
            row[0]
            for row in store.query_all(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "phase4_runtime_commands" in tables
        assert "phase4_run_requests" in tables
        assert "phase4_execution_results" in tables
        execution_columns = {
            row[1] for row in store.query_all("PRAGMA table_info(executions)")
        }
        assert "run_request_id" in execution_columns
        execution_indexes = {
            row[1]: bool(row[2])
            for row in store.query_all("PRAGMA index_list(executions)")
        }
        assert execution_indexes["ux_executions_run_request_id"] is True
    finally:
        store.close()



def test_newer_schema_version_is_rejected(tmp_path):
    database = tmp_path / "future.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        """
        CREATE TABLE astra_schema (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT INTO astra_schema VALUES (1, ?, 'future')",
        (CURRENT_SCHEMA_VERSION + 1,),
    )
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="newer than supported"):
        AstraStore(database)


def test_schema_v12_removes_abandoned_conversation_resolution_table(tmp_path):
    database = tmp_path / "schema-v12-conversation-resolution.sqlite3"
    store = AstraStore(database)
    store.close()

    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE conversation_resolutions (resolution_id TEXT PRIMARY KEY)"
    )
    connection.execute(
        "UPDATE astra_schema SET version = 12 WHERE singleton = 1"
    )
    connection.commit()
    connection.close()

    migrated = AstraStore(database)
    try:
        assert migrated.schema_version == CURRENT_SCHEMA_VERSION
        assert migrated.query_one(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'conversation_resolutions'
            """
        ) is None
    finally:
        migrated.close()


def test_schema_v15_marks_legacy_conversation_contract_nonrecoverable(tmp_path):
    database = tmp_path / "schema-v15-legacy-conversation.sqlite3"
    store = AstraStore(database)
    legacy_contract = _contract().model_dump(mode="json", exclude_none=True)
    legacy_contract["execution_type"] = "conversation"
    now = "2026-07-24T00:00:00+00:00"
    with store.transaction() as connection:
        connection.execute(
            "UPDATE astra_schema SET version = 15 WHERE singleton = 1"
        )
        connection.execute(
            """
            INSERT INTO phase3_tasks(
                task_id, contract_json, contract_id, contract_version,
                contract_hash, state, version, current_attempt_id,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'waiting_input', 1, ?, ?, ?)
            """,
            (
                "task:legacy-conversation",
                json.dumps(legacy_contract, sort_keys=True),
                legacy_contract["contract_id"],
                legacy_contract["contract_version"],
                legacy_contract["contract_hash"],
                "attempt:legacy-conversation",
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO phase3_attempts(
                attempt_id, task_id, ordinal, state, version, created_at
            ) VALUES (?, ?, 1, 'waiting', 1, ?)
            """,
            ("attempt:legacy-conversation", "task:legacy-conversation", now),
        )
        connection.execute(
            """
            INSERT INTO phase4_runtime_commands(
                command_id, command_type, payload_hash, result_json, created_at
            ) VALUES (?, 'submit_task', 'sha256:legacy', '{}', ?)
            """,
            ("submit:legacy-conversation", now),
        )
        connection.execute(
            """
            INSERT INTO phase4_run_requests(
                run_request_id, task_id, attempt_id, reason, state,
                priority, ready_at, created_by_command_id, created_at
            ) VALUES (?, ?, ?, 'interaction_resolved', 'pending', 0, ?, ?, ?)
            """,
            (
                "run-request:legacy-conversation",
                "task:legacy-conversation",
                "attempt:legacy-conversation",
                now,
                "submit:legacy-conversation",
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO interactions(
                interaction_id, execution_id, task_id, attempt_id, kind,
                purpose, prompt, status, version, payload_json, created_at
            ) VALUES (?, NULL, ?, ?, 'user_input', 'clarification', ?,
                      'pending', 1, '{}', ?)
            """,
            (
                "interaction:legacy-conversation",
                "task:legacy-conversation",
                "attempt:legacy-conversation",
                "legacy prompt",
                now,
            ),
        )
    store.close()

    migrated = AstraStore(database)
    try:
        task = migrated.query_one(
            "SELECT state, termination_reason, contract_json FROM phase3_tasks WHERE task_id = ?",
            ("task:legacy-conversation",),
        )
        attempt = migrated.query_one(
            "SELECT state, termination_reason FROM phase3_attempts WHERE attempt_id = ?",
            ("attempt:legacy-conversation",),
        )
        request = migrated.query_one(
            "SELECT state, termination_reason FROM phase4_run_requests WHERE run_request_id = ?",
            ("run-request:legacy-conversation",),
        )
        interaction = migrated.query_one(
            "SELECT status FROM interactions WHERE interaction_id = ?",
            ("interaction:legacy-conversation",),
        )
        assert json.loads(task["contract_json"])["execution_type"] == "conversation"
        assert (task["state"], task["termination_reason"]) == (
            "failed",
            "contract_runtime_incompatible",
        )
        assert (attempt["state"], attempt["termination_reason"]) == (
            "failed",
            "contract_runtime_incompatible",
        )
        assert (request["state"], request["termination_reason"]) == (
            "cancelled",
            "contract_runtime_incompatible",
        )
        assert interaction["status"] == "cancelled"
        assert migrated.query_one(
            "SELECT COUNT(*) FROM executions WHERE task_id = ?",
            ("task:legacy-conversation",),
        )[0] == 0
    finally:
        migrated.close()


def test_schema_v1_migrates_execution_relation_without_rewriting_history(
    tmp_path,
):
    database = tmp_path / "schema-v1.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    AstraStore._migrate_legacy_to_v1(connection)
    connection.execute(
        """
        CREATE TABLE astra_schema (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO astra_schema VALUES (1, 1, 'v1')")
    connection.execute(
        """
        INSERT INTO executions(
            execution_id, task_id, attempt_id, status, started_at
        ) VALUES ('v1-execution', 'v1-task', 'v1-attempt', 'running', 'v1')
        """
    )
    connection.commit()
    connection.close()

    store = AstraStore(database)
    try:
        assert store.schema_version == CURRENT_SCHEMA_VERSION
        execution = store.get_execution("v1-execution")
        assert execution is not None
        assert execution["run_request_id"] is None
        assert "termination_reason" in {
            row[1] for row in store.query_all("PRAGMA table_info(phase3_tasks)")
        }
        assert "termination_reason" in {
            row[1] for row in store.query_all("PRAGMA table_info(phase3_attempts)")
        }
        assert store.query_one(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'index' AND name = 'ux_executions_run_request_id'
            """
        )
    finally:
        store.close()


def test_submit_task_atomically_creates_initial_lifecycle(tmp_path):
    store = AstraStore(tmp_path / "submit.sqlite3")
    runtime = TaskRuntime(store)
    try:
        result = runtime.submit_task(
            command_id="submit-1",
            task_id="task-1",
            contract=_contract(),
            priority=4,
        )

        task = store.query_one(
            "SELECT * FROM phase3_tasks WHERE task_id = ?", (result.task_id,)
        )
        attempt = store.query_one(
            "SELECT * FROM phase3_attempts WHERE attempt_id = ?",
            (result.attempt_id,),
        )
        request = store.query_one(
            "SELECT * FROM phase4_run_requests WHERE run_request_id = ?",
            (result.run_request_id,),
        )
        command = store.query_one(
            "SELECT * FROM phase4_runtime_commands WHERE command_id = ?",
            (result.command_id,),
        )

        assert task["state"] == "pending"
        assert task["version"] == 1
        assert task["current_attempt_id"] == result.attempt_id
        assert attempt["state"] == "active"
        assert attempt["version"] == 1
        assert request["state"] == "pending"
        assert request["reason"] == "task_submitted"
        assert request["priority"] == 4
        assert request["created_by_command_id"] == result.command_id
        assert json.loads(command["result_json"]) == result.model_dump(mode="json")
    finally:
        store.close()


@pytest.mark.parametrize(
    "failing_table",
    ("phase3_tasks", "phase3_attempts", "phase4_run_requests"),
)
def test_submit_task_rolls_back_if_any_lifecycle_insert_fails(
    tmp_path, failing_table
):
    store = AstraStore(tmp_path / f"rollback-{failing_table}.sqlite3")
    runtime = TaskRuntime(store)
    store.connection.execute(
        f"""
        CREATE TRIGGER fail_submission_insert
        BEFORE INSERT ON {failing_table}
        BEGIN
            SELECT RAISE(ABORT, 'forced submission failure');
        END
        """
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="forced submission failure"):
            runtime.submit_task(
                command_id="submit-rollback",
                task_id="task-rollback",
                contract=_contract(),
            )

        assert _count(store, "phase3_tasks") == 0
        assert _count(store, "phase3_attempts") == 0
        assert _count(store, "phase4_run_requests") == 0
        assert _count(store, "phase4_runtime_commands") == 0
    finally:
        store.close()


def test_submit_task_survives_process_restart_and_replays_original_result(tmp_path):
    database = tmp_path / "restart.sqlite3"
    contract = _contract()
    contract_path = tmp_path / "contract.json"
    contract_path.write_text(contract.model_dump_json())

    store = AstraStore(database)
    runtime = TaskRuntime(store)
    original = runtime.submit_task(
        command_id="submit-restart",
        task_id="task-restart",
        contract=contract,
    )
    store.close()


    script = """
import sys
from astra.phase3.task_contract import TaskContract
from astra.runtime import TaskRuntime
from astra.storage import AstraStore

store = AstraStore(sys.argv[1])
contract = TaskContract.model_validate_json(open(sys.argv[2]).read())
result = TaskRuntime(store).submit_task(
    command_id='submit-restart',
    task_id='task-restart',
    contract=contract,
)
print(result.model_dump_json())
store.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database), str(contract_path)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout) == original.model_dump(mode="json")

    audit = AstraStore(database)
    try:
        assert _count(audit, "phase3_tasks") == 1
        assert _count(audit, "phase3_attempts") == 1
        assert _count(audit, "phase4_run_requests") == 1
        assert _count(audit, "phase4_runtime_commands") == 1
    finally:
        audit.close()


def test_claiming_explicit_run_request_cannot_consume_older_recovery_work(tmp_path):
    store = AstraStore(tmp_path / "foreground-run-request.sqlite3")
    runtime = TaskRuntime(store)
    limits = {
        "max_attempts": 1,
        "task_deadline": "2099-01-01T00:00:00Z",
        "max_executions_per_attempt": 2,
        "max_feedback_cycles": 0,
        "max_reconcile_cycles": 0,
    }
    contract_a = _independent_contract(
        contract_id="contract:old-recovery",
        task_type="old_recovery",
        completion_contract_id="completion:old-recovery",
        limits=limits,
    )
    contract_b = _independent_contract(
        contract_id="contract:new-message",
        task_type="new_message",
        completion_contract_id="completion:new-message",
        limits=limits,
    )
    old = runtime.submit_task(
        command_id="submit:old-recovery",
        task_id="task:old-recovery",
        contract=contract_a,
    )
    new = runtime.submit_task(
        command_id="submit:new-message",
        task_id="task:new-message",
        contract=contract_b,
    )

    claim = runtime.claim_and_start_execution(
        lease_owner_id="worker:foreground",
        lease_duration_seconds=30,
        run_request_id=new.run_request_id,
    )

    assert claim is not None
    assert claim.task_id == new.task_id
    assert claim.attempt_id == new.attempt_id
    assert claim.run_request_id == new.run_request_id
    assert store.query_one(
        "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
        (old.run_request_id,),
    )["state"] == "pending"
    store.close()


def test_submit_task_command_identity_conflict_is_stable(tmp_path):
    store = AstraStore(tmp_path / "identity.sqlite3")
    runtime = TaskRuntime(store)
    try:
        runtime.submit_task(
            command_id="submit-identity",
            task_id="task-identity",
            contract=_contract(),
            priority=1,
        )
        with pytest.raises(CommandIdentityConflict, match="identity_conflict") as error:
            runtime.submit_task(
                command_id="submit-identity",
                task_id="task-identity",
                contract=_contract(),
                priority=2,
            )
        assert error.value.code == "identity_conflict"
        assert _count(store, "phase3_tasks") == 1
        assert _count(store, "phase3_attempts") == 1
        assert _count(store, "phase4_run_requests") == 1
    finally:
        store.close()


def test_two_connections_submit_same_command_only_create_one_lifecycle(tmp_path):
    database = tmp_path / "concurrent.sqlite3"
    first_store = AstraStore(database)
    second_store = AstraStore(database)
    first_runtime = TaskRuntime(first_store)
    second_runtime = TaskRuntime(second_store)
    barrier = threading.Barrier(2)
    contract = _contract()

    def submit(runtime: TaskRuntime):
        barrier.wait()
        return runtime.submit_task(
            command_id="submit-concurrent",
            task_id="task-concurrent",
            contract=contract,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, (first_runtime, second_runtime)))
        assert results[0] == results[1]
    finally:
        first_store.close()
        second_store.close()

    audit = AstraStore(database)
    try:
        assert _count(audit, "phase3_tasks") == 1
        assert _count(audit, "phase3_attempts") == 1
        assert _count(audit, "phase4_run_requests") == 1
        assert _count(audit, "phase4_runtime_commands") == 1
    finally:
        audit.close()


def test_claim_and_start_execution_commits_one_atomic_lifecycle(tmp_path):
    store = AstraStore(tmp_path / "claim.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-claim",
        task_id="task-claim",
        contract=_contract(),
        ready_at=READY_AT,
    )
    try:
        result = runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            execution_id="execution-claim",
            now=CLAIM_TIME,
        )

        assert result is not None
        assert result.run_request_id == submission.run_request_id
        assert result.run_request_state == "claimed"
        assert result.execution_id == "execution-claim"
        request = store.query_one(
            "SELECT * FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        execution = store.get_execution("execution-claim")
        assert request["state"] == "claimed"
        assert execution is not None
        assert execution["status"] == "running"
        assert execution["run_request_id"] == submission.run_request_id
    finally:
        store.close()


def test_claim_and_start_execution_leaves_future_request_pending(tmp_path):
    store = AstraStore(tmp_path / "not-ready.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-not-ready",
        task_id="task-not-ready",
        contract=_contract(),
        ready_at="2026-07-20T13:00:00+00:00",
    )
    try:
        assert runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            now=CLAIM_TIME,
        ) is None
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        assert request["state"] == "pending"
        assert _count(store, "executions") == 0
    finally:
        store.close()


@pytest.mark.parametrize(
    ("table", "identity_column", "state", "expected_code"),
    (
        ("phase3_tasks", "task_id", "succeeded", "task_not_executable"),
        ("phase3_attempts", "attempt_id", "completed", "attempt_not_active"),
    ),
)
def test_claim_and_start_execution_cancels_ineligible_lifecycle_state(
    tmp_path,
    table,
    identity_column,
    state,
    expected_code,
):
    store = AstraStore(tmp_path / f"ineligible-{table}.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id=f"submit-ineligible-{table}",
        task_id=f"task-ineligible-{table}",
        contract=_contract(),
        ready_at=READY_AT,
    )
    identity = (
        submission.task_id if identity_column == "task_id" else submission.attempt_id
    )
    trigger = (
        "phase4_cancel_pending_requests_for_terminal_task"
        if table == "phase3_tasks"
        else "phase4_cancel_pending_requests_for_terminal_attempt"
    )
    store.connection.execute(f"DROP TRIGGER {trigger}")
    store.connection.execute(
        f"UPDATE {table} SET state = ? WHERE {identity_column} = ?",
        (state, identity),
    )
    try:
        assert runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            now=CLAIM_TIME,
        ) is None
        request = store.query_one(
            """
            SELECT state, termination_reason
            FROM phase4_run_requests WHERE run_request_id = ?
            """,
            (submission.run_request_id,),
        )
        assert (request["state"], request["termination_reason"]) == (
            "cancelled",
            expected_code,
        )
        assert _count(store, "executions") == 0
        checkpoint = store.query_one(
            """
            SELECT boundary, envelope_json FROM phase4_checkpoints
            WHERE run_request_id = ? AND boundary = 'run_request_cancelled'
            """,
            (submission.run_request_id,),
        )
        assert checkpoint is not None
        assert json.loads(checkpoint["envelope_json"])["references"] == {
            "termination_reason": expected_code
        }
    finally:
        store.close()


def test_claim_and_start_execution_enforces_task_deadline(tmp_path):
    store = AstraStore(tmp_path / "deadline.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-deadline",
        task_id="task-deadline",
        contract=_deadline_contract(
            contract_id="contract-task-deadline",
            task_deadline="2026-07-21T00:00:00Z",
            attempt_deadline=None,
        ),
        ready_at=READY_AT,
    )
    try:
        assert runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            now=datetime(2026, 7, 21, tzinfo=timezone.utc),
        ) is None
        task = store.query_one(
            """
            SELECT state, version, termination_reason
            FROM phase3_tasks WHERE task_id = ?
            """,
            (submission.task_id,),
        )
        attempt = store.query_one(
            """
            SELECT state, version, termination_reason
            FROM phase3_attempts WHERE attempt_id = ?
            """,
            (submission.attempt_id,),
        )
        request = store.query_one(
            """
            SELECT state, termination_reason
            FROM phase4_run_requests WHERE run_request_id = ?
            """,
            (submission.run_request_id,),
        )
        assert (task["state"], task["version"], task["termination_reason"]) == (
            "failed",
            2,
            "task_deadline_exceeded",
        )
        assert (
            attempt["state"],
            attempt["version"],
            attempt["termination_reason"],
        ) == ("failed", 2, "task_deadline_exceeded")
        assert (request["state"], request["termination_reason"]) == (
            "cancelled",
            "task_deadline_exceeded",
        )
        assert _count(store, "executions") == 0
    finally:
        store.close()


def test_claim_and_start_execution_exhausts_attempt_at_attempt_deadline(
    tmp_path,
):
    store = AstraStore(tmp_path / "attempt-deadline.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-attempt-deadline",
        task_id="task-attempt-deadline",
        contract=_deadline_contract(
            contract_id="contract-attempt-deadline",
            task_deadline="2026-07-22T00:00:00Z",
            attempt_deadline="2026-07-21T00:00:00Z",
        ),
        ready_at=READY_AT,
    )
    try:
        assert runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            now=datetime(2026, 7, 21, tzinfo=timezone.utc),
        ) is None
        task = store.query_one(
            """
            SELECT state, version, termination_reason
            FROM phase3_tasks WHERE task_id = ?
            """,
            (submission.task_id,),
        )
        attempt = store.query_one(
            """
            SELECT state, version, termination_reason
            FROM phase3_attempts WHERE attempt_id = ?
            """,
            (submission.attempt_id,),
        )
        assert (task["state"], task["version"], task["termination_reason"]) == (
            "pending",
            1,
            None,
        )
        assert (
            attempt["state"],
            attempt["version"],
            attempt["termination_reason"],
        ) == ("exhausted", 2, "attempt_deadline_exceeded")
        request = store.query_one(
            """
            SELECT state, termination_reason
            FROM phase4_run_requests WHERE run_request_id = ?
            """,
            (submission.run_request_id,),
        )
        assert (request["state"], request["termination_reason"]) == (
            "cancelled",
            "attempt_deadline_exceeded",
        )
        assert _count(store, "executions") == 0
    finally:
        store.close()


def test_claim_cleans_poison_request_and_continues_to_next_legal_request(tmp_path):
    store = AstraStore(tmp_path / "poison-queue.sqlite3")
    runtime = TaskRuntime(store)
    poison = runtime.submit_task(
        command_id="submit-poison",
        task_id="task-poison",
        contract=_contract(),
        priority=10,
        ready_at=READY_AT,
    )
    legal = runtime.submit_task(
        command_id="submit-legal-after-poison",
        task_id="task-legal-after-poison",
        contract=_contract(),
        priority=0,
        ready_at=READY_AT,
    )
    store.connection.execute(
        "DROP TRIGGER phase4_cancel_pending_requests_for_terminal_task"
    )
    store.connection.execute(
        "UPDATE phase3_tasks SET state = 'succeeded' WHERE task_id = ?",
        (poison.task_id,),
    )
    try:
        claim = runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            execution_id="execution-after-poison",
            now=CLAIM_TIME,
        )
        assert claim is not None
        assert claim.run_request_id == legal.run_request_id
        poison_request = store.query_one(
            """
            SELECT state, termination_reason FROM phase4_run_requests
            WHERE run_request_id = ?
            """,
            (poison.run_request_id,),
        )
        assert (poison_request["state"], poison_request["termination_reason"]) == (
            "cancelled",
            "task_not_executable",
        )
        assert _count(store, "executions") == 1
        assert store.query_one(
            "SELECT run_request_id FROM executions WHERE execution_id = ?",
            (claim.execution_id,),
        )["run_request_id"] == legal.run_request_id
        assert store.query_one(
            """
            SELECT COUNT(*) FROM phase4_checkpoints
            WHERE run_request_id = ? AND boundary = 'run_request_cancelled'
            """,
            (poison.run_request_id,),
        )[0] == 1
    finally:
        store.close()


def test_task_deadline_failure_is_atomic_with_attempt_failure(tmp_path):
    store = AstraStore(tmp_path / "deadline-atomic.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-deadline-atomic",
        task_id="task-deadline-atomic",
        contract=_deadline_contract(
            contract_id="contract-task-deadline-atomic",
            task_deadline="2026-07-21T00:00:00Z",
            attempt_deadline=None,
        ),
        ready_at=READY_AT,
    )
    store.connection.execute(
        """
        CREATE TRIGGER fail_deadline_attempt_update
        BEFORE UPDATE ON phase3_attempts
        WHEN NEW.state = 'failed'
        BEGIN
            SELECT RAISE(ABORT, 'forced deadline attempt failure');
        END
        """
    )
    try:
        with pytest.raises(
            sqlite3.IntegrityError, match="forced deadline attempt failure"
        ):
            runtime.claim_and_start_execution(
                lease_owner_id=LEASE_OWNER_ID,
                lease_duration_seconds=LEASE_DURATION_SECONDS,
                now=datetime(2026, 7, 21, tzinfo=timezone.utc)
            )
        task = store.query_one(
            """
            SELECT state, version, termination_reason
            FROM phase3_tasks WHERE task_id = ?
            """,
            (submission.task_id,),
        )
        attempt = store.query_one(
            """
            SELECT state, version, termination_reason
            FROM phase3_attempts WHERE attempt_id = ?
            """,
            (submission.attempt_id,),
        )
        assert (task["state"], task["version"], task["termination_reason"]) == (
            "pending",
            1,
            None,
        )
        assert (
            attempt["state"],
            attempt["version"],
            attempt["termination_reason"],
        ) == ("active", 1, None)
    finally:
        store.close()



def test_execution_insert_failure_rolls_back_claim(tmp_path):
    store = AstraStore(tmp_path / "claim-rollback.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-claim-rollback",
        task_id="task-claim-rollback",
        contract=_contract(),
        ready_at=READY_AT,
    )
    store.connection.execute(
        """
        CREATE TRIGGER fail_execution_insert
        BEFORE INSERT ON executions
        BEGIN
            SELECT RAISE(ABORT, 'forced execution failure');
        END
        """
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="forced execution failure"):
            runtime.claim_and_start_execution(
                lease_owner_id=LEASE_OWNER_ID,
                lease_duration_seconds=LEASE_DURATION_SECONDS,
                now=CLAIM_TIME,
            )
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        assert request["state"] == "pending"
        assert _count(store, "executions") == 0
    finally:
        store.close()


def test_repeated_claim_does_not_create_a_second_execution(tmp_path):
    store = AstraStore(tmp_path / "repeat-claim.sqlite3")
    runtime = TaskRuntime(store)
    runtime.submit_task(
        command_id="submit-repeat-claim",
        task_id="task-repeat-claim",
        contract=_contract(),
        ready_at=READY_AT,
    )
    try:
        first = runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            execution_id="execution-repeat-claim",
            now=CLAIM_TIME,
        )
        second = runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            execution_id="execution-repeat-claim-2",
            now=CLAIM_TIME,
        )
        assert first is not None
        assert second is None
        assert _count(store, "executions") == 1
    finally:
        store.close()


def test_two_connections_competing_for_claim_create_one_execution(tmp_path):
    database = tmp_path / "concurrent-claim.sqlite3"
    first_store = AstraStore(database)
    second_store = AstraStore(database)
    first_runtime = TaskRuntime(first_store)
    second_runtime = TaskRuntime(second_store)
    first_runtime.submit_task(
        command_id="submit-concurrent-claim",
        task_id="task-concurrent-claim",
        contract=_contract(),
        ready_at=READY_AT,
    )
    barrier = threading.Barrier(2)

    def claim(runtime: TaskRuntime, execution_id: str):
        barrier.wait()
        return runtime.claim_and_start_execution(
            lease_owner_id=LEASE_OWNER_ID,
            lease_duration_seconds=LEASE_DURATION_SECONDS,
            execution_id=execution_id,
            now=CLAIM_TIME,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = (
                pool.submit(claim, first_runtime, "execution-concurrent-1"),
                pool.submit(claim, second_runtime, "execution-concurrent-2"),
            )
            results = [future.result() for future in futures]
        assert sum(result is not None for result in results) == 1
    finally:
        first_store.close()
        second_store.close()

    audit = AstraStore(database)
    try:
        assert _count(audit, "executions") == 1
        request = audit.query_one("SELECT state FROM phase4_run_requests")
        assert request["state"] == "claimed"
    finally:
        audit.close()


def test_claimed_execution_survives_restart_and_cannot_be_reclaimed(tmp_path):
    database = tmp_path / "claim-restart.sqlite3"
    store = AstraStore(database)
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-claim-restart",
        task_id="task-claim-restart",
        contract=_contract(),
        ready_at=READY_AT,
    )
    original = runtime.claim_and_start_execution(
        lease_owner_id=LEASE_OWNER_ID,
        lease_duration_seconds=LEASE_DURATION_SECONDS,
        execution_id="execution-claim-restart",
        now=CLAIM_TIME,
    )
    assert original is not None
    store.close()

    script = """
import sys
from astra.runtime import TaskRuntime
from astra.storage import AstraStore

store = AstraStore(sys.argv[1])
result = TaskRuntime(store).claim_and_start_execution(
    lease_owner_id='test-phase4-worker', lease_duration_seconds=30.0
)
print('none' if result is None else result.model_dump_json())
store.close()
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(database)],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "none"

    audit = AstraStore(database)
    try:
        request = audit.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        execution = audit.get_execution(original.execution_id)
        assert request["state"] == "claimed"
        assert execution is not None
        assert execution["run_request_id"] == submission.run_request_id
        assert _count(audit, "executions") == 1
    finally:
        audit.close()


def _execution_result(
    execution_id: str,
    *,
    status: ExecutionStatus = ExecutionStatus.SUCCEEDED,
    validated: bool = True,
) -> ExecutionResult:
    return ExecutionResult(
        execution_id=execution_id,
        status=status,
        agent_turn_finished=True,
        task_outcome_validated=validated,
        assistant_output="deterministic",
        submitted_result={"accepted": validated},
        result_receipt={
            "receipt_id": f"result-receipt:{execution_id}",
            "valid": validated,
            "evidence_refs": [],
            "receipt_refs": [],
        },
        termination_reason="deterministic_test_result",
    )


def test_record_execution_result_is_idempotent_and_immutable(tmp_path):
    store = AstraStore(tmp_path / "result-command.sqlite3")
    runtime = TaskRuntime(store)
    runtime.submit_task(
        command_id="submit-result-command",
        task_id="task-result-command",
        contract=_contract(),
        ready_at=READY_AT,
    )
    claim = runtime.claim_and_start_execution(
        lease_owner_id=LEASE_OWNER_ID,
        lease_duration_seconds=315_360_000.0,
        execution_id="execution-result-command",
        now=CLAIM_TIME,
    )
    assert claim is not None
    result = _execution_result(claim.execution_id)
    try:
        first = runtime.record_execution_result(
            command_id="record-result-command",
            execution_id=claim.execution_id,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            result=result,
        )
        replay = runtime.record_execution_result(
            command_id="record-result-command",
            execution_id=claim.execution_id,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            result=result,
        )
        same_result_new_command = runtime.record_execution_result(
            command_id="record-result-command-retry",
            execution_id=claim.execution_id,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
            result=result,
        )

        assert first == replay == same_result_new_command == result
        assert runtime.get_execution_result(claim.execution_id) == result
        assert _count(store, "phase4_execution_results") == 1
        assert _count(store, "phase4_runtime_commands") == 3
        task = store.query_one(
            "SELECT state, version FROM phase3_tasks WHERE task_id = ?",
            (claim.task_id,),
        )
        attempt = store.query_one(
            "SELECT state, version FROM phase3_attempts WHERE attempt_id = ?",
            (claim.attempt_id,),
        )
        assert (task["state"], task["version"]) == ("pending", 1)
        assert (attempt["state"], attempt["version"]) == ("active", 1)

        with pytest.raises(CommandIdentityConflict, match="identity_conflict"):
            runtime.record_execution_result(
                command_id="record-result-command",
                execution_id=claim.execution_id,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
                result=_execution_result(claim.execution_id, validated=False),
            )
        with pytest.raises(
            ExecutionResultConflict, match="execution_result_conflict"
        ):
            runtime.record_execution_result(
                command_id="record-result-conflict",
                execution_id=claim.execution_id,
                lease_owner_id=claim.lease_owner_id,
                lease_token=claim.lease_token,
                result=_execution_result(claim.execution_id, validated=False),
            )
        assert _count(store, "phase4_execution_results") == 1
    finally:
        store.close()


def test_persisted_execution_result_survives_restart(tmp_path):
    database = tmp_path / "result-restart.sqlite3"
    store = AstraStore(database)
    runtime = TaskRuntime(store)
    runtime.submit_task(
        command_id="submit-result-restart",
        task_id="task-result-restart",
        contract=_contract(),
        ready_at=READY_AT,
    )
    claim = runtime.claim_and_start_execution(
        lease_owner_id=LEASE_OWNER_ID,
        lease_duration_seconds=315_360_000.0,
        execution_id="execution-result-restart",
        now=CLAIM_TIME,
    )
    assert claim is not None
    expected = _execution_result(claim.execution_id)
    runtime.record_execution_result(
        command_id="record-result-restart",
        execution_id=claim.execution_id,
        lease_owner_id=claim.lease_owner_id,
        lease_token=claim.lease_token,
        result=expected,
    )
    store.close()

    reopened = AstraStore(database)
    try:
        assert TaskRuntime(reopened).get_execution_result(claim.execution_id) == expected
    finally:
        reopened.close()
