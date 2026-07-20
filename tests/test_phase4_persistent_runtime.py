from __future__ import annotations

import asyncio
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
from astra.phase3.canonical import sha256_digest
from astra.phase3.completion import (
    CompletionContract,
    CompletionRequirement,
    CompletionStatus,
    EvidenceSnapshot,
    EvaluatorRef,
    RequirementEvaluation,
    RequirementStatus,
    SubmittedResult,
)
from astra.phase3.governance import GovernanceStore, RuntimeGovernanceCore
from astra.phase3.policy import DecisionApplication, PolicyAction
from astra.phase3.task_contract import TaskContract
from astra.runtime import (
    CommandIdentityConflict,
    DeterministicFakeExecutor,
    ExecutionEligibilityError,
    ExecutionResultConflict,
    RunRequestExecutionConflict,
    SingleWorker,
    TaskRuntime,
)
from astra.storage import CURRENT_SCHEMA_VERSION, AstraStore


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "phase3_contract_round2.json"
CLAIM_TIME = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
READY_AT = "2026-07-20T00:00:00+00:00"


def _contract() -> TaskContract:
    fixture = json.loads(FIXTURE_PATH.read_text())
    return TaskContract.materialize(fixture["scenarios"][0]["task_contract"])


def _governed_contract() -> TaskContract:
    fixture = json.loads(FIXTURE_PATH.read_text())
    contract = dict(fixture["scenarios"][0]["task_contract"])
    contract.update(
        {
            "contract_id": "contract-configured-runtime-test",
            "task_type": "configured_runtime_test",
            "execution_type": "deterministic_executor",
            "objective": {
                "description": "Evaluate the submitted result under its contract."
            },
            "allowed_capabilities": [],
            "resolved_tools": [],
            "constraints": [],
            "authorized_effects": [],
            "approval_requirements": [],
            "completion_contract_ref": {
                "contract_id": "completion-configured-runtime-test",
                "contract_version": "1",
            },
        }
    )
    return TaskContract.materialize(contract)


class _ConfiguredOutcomeEvaluator:
    evaluator_id = "tests.configured_outcome"
    evaluator_version = "1"

    def evaluate(
        self,
        requirement: CompletionRequirement,
        submitted_result: SubmittedResult,
        evidence_snapshot: EvidenceSnapshot,
    ) -> RequirementEvaluation:
        field = str(requirement.configuration["field"])
        expected = requirement.configuration["equals"]
        satisfied = submitted_result.outcome.get(field) == expected
        return RequirementEvaluation(
            evaluation_id="evaluation:"
            + sha256_digest(
                {
                    "requirement_id": requirement.requirement_id,
                    "evaluator_id": self.evaluator_id,
                    "evaluator_version": self.evaluator_version,
                    "submitted_result_id": submitted_result.submitted_result_id,
                    "evidence_snapshot_id": evidence_snapshot.evidence_snapshot_id,
                }
            ),
            requirement_id=requirement.requirement_id,
            evaluator_id=self.evaluator_id,
            evaluator_version=self.evaluator_version,
            submitted_result_id=submitted_result.submitted_result_id,
            evidence_snapshot_id=evidence_snapshot.evidence_snapshot_id,
            status=(
                RequirementStatus.SATISFIED
                if satisfied
                else RequirementStatus.UNSATISFIED
            ),
        )


def _completion_contract() -> CompletionContract:
    task_contract = _governed_contract()
    return CompletionContract(
        contract_id=task_contract.completion_contract_ref.contract_id,
        contract_version=task_contract.completion_contract_ref.contract_version,
        task_type=task_contract.task_type,
        requirements=(
            CompletionRequirement(
                requirement_id="configured_outcome_matches",
                description="The configured submitted outcome value matches.",
                evaluator=EvaluatorRef(
                    evaluator_id=_ConfiguredOutcomeEvaluator.evaluator_id,
                    evaluator_version=_ConfiguredOutcomeEvaluator.evaluator_version,
                ),
                configuration={
                    "field": "governance_verdict",
                    "equals": "pass",
                },
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


def test_schema_v1_migrates_execution_relation_without_rewriting_history(
    tmp_path,
):
    database = tmp_path / "schema-v1.sqlite3"
    contract = _contract()
    governance_store = GovernanceStore(database)
    RuntimeGovernanceCore(governance_store).create_task(
        contract,
        task_id="v1-task",
        attempt_id="v1-attempt",
    )
    governance_store.close()

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
        assert runtime.claim_and_start_execution(now=CLAIM_TIME) is None
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
def test_claim_and_start_execution_rejects_ineligible_lifecycle_state(
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
    store.connection.execute(
        f"UPDATE {table} SET state = ? WHERE {identity_column} = ?",
        (state, identity),
    )
    try:
        with pytest.raises(ExecutionEligibilityError, match=expected_code) as error:
            runtime.claim_and_start_execution(now=CLAIM_TIME)
        assert error.value.code == expected_code
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        assert request["state"] == "pending"
        assert _count(store, "executions") == 0
    finally:
        store.close()


def test_claim_and_start_execution_enforces_task_deadline(tmp_path):
    store = AstraStore(tmp_path / "deadline.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-deadline",
        task_id="task-deadline",
        contract=_contract(),
        ready_at=READY_AT,
    )
    try:
        with pytest.raises(
            ExecutionEligibilityError, match="task_deadline_exceeded"
        ) as error:
            runtime.claim_and_start_execution(
                now=datetime(2026, 7, 21, tzinfo=timezone.utc)
            )
        assert error.value.code == "task_deadline_exceeded"
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        assert request["state"] == "pending"
        assert _count(store, "executions") == 0
    finally:
        store.close()


def test_claim_and_start_execution_enforces_execution_budget(tmp_path):
    store = AstraStore(tmp_path / "execution-budget.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-budget",
        task_id="task-budget",
        contract=_contract(),
        ready_at=READY_AT,
    )
    for ordinal in range(_contract().limits.max_executions_per_attempt):
        store.start_execution(
            f"historical-execution-{ordinal}",
            submission.task_id,
            submission.attempt_id,
        )
    try:
        with pytest.raises(
            ExecutionEligibilityError, match="execution_budget_exhausted"
        ) as error:
            runtime.claim_and_start_execution(now=CLAIM_TIME)
        assert error.value.code == "execution_budget_exhausted"
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        assert request["state"] == "pending"
        assert _count(store, "executions") == 4
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
            runtime.claim_and_start_execution(now=CLAIM_TIME)
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
            execution_id="execution-repeat-claim",
            now=CLAIM_TIME,
        )
        second = runtime.claim_and_start_execution(
            execution_id="execution-repeat-claim-2",
            now=CLAIM_TIME,
        )
        assert first is not None
        assert second is None
        assert _count(store, "executions") == 1
    finally:
        store.close()


def test_unique_run_request_conflict_has_stable_error_semantics(tmp_path):
    store = AstraStore(tmp_path / "claim-unique.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-claim-unique",
        task_id="task-claim-unique",
        contract=_contract(),
        ready_at=READY_AT,
    )
    runtime.claim_and_start_execution(
        execution_id="execution-claim-unique",
        now=CLAIM_TIME,
    )
    store.connection.execute(
        """
        UPDATE phase4_run_requests SET state = 'pending'
        WHERE run_request_id = ?
        """,
        (submission.run_request_id,),
    )
    try:
        with pytest.raises(
            RunRequestExecutionConflict,
            match="run_request_execution_conflict",
        ) as error:
            runtime.claim_and_start_execution(
                execution_id="execution-claim-unique-2",
                now=CLAIM_TIME,
            )
        assert error.value.code == "run_request_execution_conflict"
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        assert request["state"] == "pending"
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
result = TaskRuntime(store).claim_and_start_execution()
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
        execution_id="execution-result-command",
        now=CLAIM_TIME,
    )
    assert claim is not None
    result = _execution_result(claim.execution_id)
    try:
        first = runtime.record_execution_result(
            command_id="record-result-command",
            execution_id=claim.execution_id,
            result=result,
        )
        replay = runtime.record_execution_result(
            command_id="record-result-command",
            execution_id=claim.execution_id,
            result=result,
        )
        same_result_new_command = runtime.record_execution_result(
            command_id="record-result-command-retry",
            execution_id=claim.execution_id,
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
                result=_execution_result(claim.execution_id, validated=False),
            )
        with pytest.raises(
            ExecutionResultConflict, match="execution_result_conflict"
        ):
            runtime.record_execution_result(
                command_id="record-result-conflict",
                execution_id=claim.execution_id,
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
        execution_id="execution-result-restart",
        now=CLAIM_TIME,
    )
    assert claim is not None
    expected = _execution_result(claim.execution_id)
    runtime.record_execution_result(
        command_id="record-result-restart",
        execution_id=claim.execution_id,
        result=expected,
    )
    store.close()

    reopened = AstraStore(database)
    try:
        assert TaskRuntime(reopened).get_execution_result(claim.execution_id) == expected
    finally:
        reopened.close()


@pytest.mark.parametrize(
    (
        "executor_status",
        "executor_claimed_validation",
        "submitted_verdict",
        "expected_action",
        "expected_task_state",
        "expected_attempt",
    ),
    (
        (
            ExecutionStatus.FAILED,
            False,
            "pass",
            PolicyAction.COMPLETE,
            "succeeded",
            "completed",
        ),
        (
            ExecutionStatus.SUCCEEDED,
            True,
            "fail",
            PolicyAction.CONTINUE_WITH_FEEDBACK,
            "running",
            "active",
        ),
    ),
)
def test_single_worker_keeps_executor_run_request_and_task_outcomes_separate(
    tmp_path,
    executor_status,
    executor_claimed_validation,
    submitted_verdict,
    expected_action,
    expected_task_state,
    expected_attempt,
):
    store = AstraStore(
        tmp_path / f"worker-{executor_status.value}-{submitted_verdict}.sqlite3"
    )
    runtime = TaskRuntime(store)
    runtime.governance_core.evaluator_registry.register(
        _ConfiguredOutcomeEvaluator()
    )
    submission = runtime.submit_task(
        command_id=f"submit-worker-{executor_status.value}-{submitted_verdict}",
        task_id=f"task-worker-{executor_status.value}-{submitted_verdict}",
        contract=_governed_contract(),
        completion_contract=_completion_contract(),
        ready_at=READY_AT,
    )

    def result_factory(invocation):
        return ExecutionResult(
            execution_id=invocation.execution_id,
            status=executor_status,
            agent_turn_finished=True,
            task_outcome_validated=executor_claimed_validation,
            assistant_output="deterministic",
            submitted_result={"governance_verdict": submitted_verdict},
            result_receipt={
                "receipt_id": f"result-receipt:{invocation.execution_id}",
                "valid": executor_claimed_validation,
                "evidence_refs": [],
                "receipt_refs": [],
            },
            termination_reason="configured_deterministic_result",
        )

    executor = DeterministicFakeExecutor(result_factory=result_factory)
    worker = SingleWorker(runtime, executor)
    try:
        run = asyncio.run(
            worker.run_once(
                execution_id=(
                    f"execution-worker-{executor_status.value}-{submitted_verdict}"
                ),
                now=CLAIM_TIME,
            )
        )

        assert run is not None
        assert run.execution_result.status == executor_status
        assert run.run_request_state == "completed"
        assert run.governance_application.application == DecisionApplication.APPLIED
        assert run.governance_evaluation.policy_decision.action == expected_action
        assert len(executor.invocations) == 1
        invocation = executor.invocations[0]
        assert invocation.task_id == submission.task_id
        assert invocation.attempt_id == submission.attempt_id
        assert invocation.user_request == _governed_contract().objective.description
        assert tuple(invocation.allowed_tools) == tuple(
            tool.tool_name for tool in _governed_contract().resolved_tools
        )

        task = store.query_one(
            "SELECT state, version FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        attempt = store.query_one(
            "SELECT state, version FROM phase3_attempts WHERE attempt_id = ?",
            (submission.attempt_id,),
        )
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        execution = store.get_execution(run.claim.execution_id)
        assert task["state"] == expected_task_state
        assert task["version"] == 3
        assert attempt["state"] == expected_attempt
        assert attempt["version"] == 2
        assert request["state"] == "completed"
        assert execution is not None
        assert execution["status"] == executor_status.value
        assert execution["ended_at"] is not None
        assert runtime.get_execution_result(run.claim.execution_id) is not None
        assert _count(store, "phase3_completion_validations") == 1
        assert _count(store, "phase3_policy_decisions") == 1
        assert _count(store, "phase3_completion_contracts") == 1

        assert asyncio.run(worker.run_once(now=CLAIM_TIME)) is None
        assert len(executor.invocations) == 1
    finally:
        store.close()


def test_single_worker_fails_closed_for_unregistered_requirement_evaluator(
    tmp_path,
):
    store = AstraStore(tmp_path / "worker-unknown-evaluator.sqlite3")
    runtime = TaskRuntime(store)
    submission = runtime.submit_task(
        command_id="submit-worker-unknown-evaluator",
        task_id="task-worker-unknown-evaluator",
        contract=_governed_contract(),
        completion_contract=_completion_contract(),
        ready_at=READY_AT,
    )

    def result_factory(invocation):
        return ExecutionResult(
            execution_id=invocation.execution_id,
            status=ExecutionStatus.SUCCEEDED,
            agent_turn_finished=True,
            task_outcome_validated=True,
            submitted_result={"governance_verdict": "pass"},
            result_receipt={
                "receipt_id": f"result-receipt:{invocation.execution_id}",
                "valid": True,
                "evidence_refs": [],
                "receipt_refs": [],
            },
            termination_reason="configured_deterministic_result",
        )

    worker = SingleWorker(
        runtime,
        DeterministicFakeExecutor(result_factory=result_factory),
    )
    try:
        run = asyncio.run(
            worker.run_once(
                execution_id="execution-worker-unknown-evaluator",
                now=CLAIM_TIME,
            )
        )

        assert run is not None
        assert (
            run.governance_evaluation.completion_validation.status
            == CompletionStatus.EVALUATOR_ERROR
        )
        assert run.governance_evaluation.policy_decision.action == PolicyAction.ESCALATE
        task = store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        request = store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        assert task["state"] == "failed"
        assert request["state"] == "completed"
    finally:
        store.close()
