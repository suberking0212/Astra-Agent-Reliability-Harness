from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from astra.phase3.governance import GovernanceStore, RuntimeGovernanceCore
from astra.phase3.task_contract import TaskContract
from astra.runtime import CommandIdentityConflict, TaskRuntime
from astra.storage import CURRENT_SCHEMA_VERSION, AstraStore


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "phase3_contract_round2.json"


def _contract() -> TaskContract:
    fixture = json.loads(FIXTURE_PATH.read_text())
    return TaskContract.materialize(fixture["scenarios"][0]["task_contract"])


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
    finally:
        store.close()


def test_phase3_database_migrates_in_place_and_repeated_migration_is_stable(
    tmp_path,
):
    database = tmp_path / "phase3.sqlite3"
    contract = _contract()
    governance_store = GovernanceStore(database)
    governance = RuntimeGovernanceCore(governance_store)
    governance.create_task(
        contract,
        task_id="historical-task",
        attempt_id="historical-attempt",
        task_version=7,
        attempt_version=3,
    )
    governance_store.close()

    first = AstraStore(database)
    try:
        historical = first.query_one(
            "SELECT * FROM phase3_tasks WHERE task_id = 'historical-task'"
        )
        assert historical is not None
        assert historical["version"] == 7
        assert historical["current_attempt_id"] == "historical-attempt"
        assert first.schema_version == CURRENT_SCHEMA_VERSION
        migrated_at = first.query_one(
            "SELECT updated_at FROM astra_schema WHERE singleton = 1"
        )[0]
    finally:
        first.close()

    second = AstraStore(database)
    try:
        assert second.schema_version == CURRENT_SCHEMA_VERSION
        assert (
            second.query_one(
                "SELECT updated_at FROM astra_schema WHERE singleton = 1"
            )[0]
            == migrated_at
        )
        assert _count(second, "phase3_tasks") == 1
        assert _count(second, "phase3_attempts") == 1
    finally:
        second.close()


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
