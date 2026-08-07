"""Domain-extension and Gateway integration tests.

The in-memory adapters are controlled component boundaries.  Reopening Astra
with the same adapter instance validates reconciliation integration, not
cross-system recovery of an independently restarted external authority.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from astra.complaint_result import ComplaintResultEvaluator
from astra.domain import ExecutionResult, ExecutionStatus, RuntimeInvocation, ToolInvocationContext
from astra.domain_extension import DomainExtension
from astra.mock_business import MockBusinessService
from astra.observations import ExternalObservation
from astra.phase3.effects import EffectNormalizationCandidate, ExternalOperation
from astra.phase3.task_contract import TaskContract, ToolAccessMode
from astra.production import ProductionConfig, ProductionRuntime
from astra.result_validator import ResultEvaluatorRegistry
from astra.tool_catalog import CatalogTool, register_catalog_tool, unregister_catalog_tool
from astra.tool_definitions import (
    ApprovalPolicy,
    EffectDefinition,
    EffectDispatchResult,
    SubjectBinding,
    ToolArgs,
    ToolDefinition,
)


class SetAccountNoteArgs(ToolArgs):
    account_id: str
    note: str


class AccountNoteNormalizer:
    normalizer_id = "crm.set_account_note"
    normalizer_version = "1"

    def normalize(
        self,
        contract: TaskContract,
        raw_request: Mapping[str, Any],
    ) -> Sequence[EffectNormalizationCandidate]:
        parameters = dict(raw_request)
        return tuple(
            EffectNormalizationCandidate(
                effect_intent_ref=intent.effect_intent_id,
                normalized_parameters=parameters,
            )
            for intent in contract.authorized_effects
            if intent.effect_type == "crm.account_note"
            and intent.subject_ref.id == parameters["account_id"]
        )


class AccountNoteAdapter:
    def __init__(self) -> None:
        self.notes: dict[str, dict[str, str]] = {}
        self.by_idempotency_key: dict[str, str] = {}
        self.response_lost_once = False
        self.confirmation_count = 0

    def invoke(self, arguments, context):
        return self.dispatch(arguments, context).result

    def dispatch(self, arguments, context):
        object_id = self.by_idempotency_key.get(str(context.idempotency_key))
        if object_id is None:
            object_id = f"note-{len(self.notes) + 1}"
            self.by_idempotency_key[str(context.idempotency_key)] = object_id
            self.notes[object_id] = {
                "account": str(arguments["account_id"]),
                "body": str(arguments["note"]),
            }
        if self.response_lost_once:
            self.response_lost_once = False
            raise RuntimeError("crm_response_lost_after_commit")
        return EffectDispatchResult(
            result={"updated": True, "note_id": object_id},
            accepted=True,
            external_object_id=object_id,
        )

    def observe(self, operation: ExternalOperation):
        object_id = operation.external_operation_id or self.by_idempotency_key.get(
            operation.idempotency_key
        )
        payload = self.notes.get(str(object_id)) if object_id is not None else None
        return (
            ExternalObservation(external_object_id=str(object_id), payload=payload)
            if payload is not None
            else None
        )

    def confirmation_matches(self, observation, arguments):
        # CRM owns this mapping; the Runtime must not compare field names itself.
        self.confirmation_count += 1
        return bool(
            observation
            and observation.get("account") == arguments.get("account_id")
            and observation.get("body") == arguments.get("note")
        )

    def replay_result(self, observation):
        return {"updated": False, "account_note": dict(observation or {})}


class CrmResultEvaluator:
    evaluator_id = "crm.account_note.result"
    evaluator_version = "1"

    def handles(self, invocation, outcome, receipts):
        return any(row.get("tool_name") == "set_account_note" for row in receipts)

    def evaluate(self, invocation, outcome, receipts):
        return {
            "crm_note_receipt_present": any(
                row.get("tool_name") == "set_account_note" for row in receipts
            ),
            "crm_note_id_present": bool(outcome.get("note_id")),
        }


class NoopExecutor:
    async def execute(self, invocation, event_sink):
        return ExecutionResult(
            execution_id=invocation.execution_id,
            status=ExecutionStatus.FAILED,
            agent_turn_finished=True,
            task_outcome_validated=False,
            termination_reason="not_used",
        )

    async def cancel(self, execution_id, reason):
        return None


def _catalog() -> CatalogTool:
    return CatalogTool(
        tool_name="set_account_note",
        capability_ref="crm.notes@1",
        access_mode=ToolAccessMode.EFFECT,
        schema={
            "description": "Set an account note.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["account_id", "note"],
                "additionalProperties": False,
            },
        },
    )


def _extension(adapter: AccountNoteAdapter, catalog: CatalogTool) -> DomainExtension:
    subject = SubjectBinding("account_id", "account", "crm.test")
    definition = ToolDefinition(
        catalog=catalog,
        input_model=SetAccountNoteArgs,
        adapter=adapter,
        subject_bindings=(subject,),
        effect=EffectDefinition(
            effect_type="crm.account_note",
            effect_type_version="1",
            authority_domain="crm.test",
            subject_binding=subject,
            normalizer=AccountNoteNormalizer(),
            approval_policy=ApprovalPolicy(
                risk_class="low", approver_policy_ref="task.requester@1"
            ),
            adapter=adapter,
        ),
    )
    return DomainExtension(
        extension_id="crm.notes",
        tool_definitions={catalog.tool_name: definition},
        result_evaluators=(CrmResultEvaluator(),),
    )


def _contract(catalog: CatalogTool) -> TaskContract:
    return TaskContract.materialize(
        {
            "schema_version": "1",
            "contract_id": "contract:crm-note",
            "contract_version": "1",
            "task_type": "crm-note",
            "execution_type": "tool_execution",
            "objective": {"description": "Set the authorized CRM note."},
            "subject_refs": [
                {"authority_domain": "crm.test", "type": "account", "id": "acct-1"}
            ],
            "input_snapshot": {
                "schema_id": "crm.note.input",
                "schema_version": "1",
                "values": {},
                "content_hash": "sha256:crm-note-input",
            },
            "allowed_capabilities": [
                {"capability_id": "crm.notes", "capability_version": "1"}
            ],
            "resolved_tools": [catalog.binding().model_dump(mode="json")],
            "constraints": [],
            "authorized_effects": [
                {
                    "effect_intent_id": "set-account-note",
                    "effect_type": "crm.account_note",
                    "effect_type_version": "1",
                    "authority_domain": "crm.test",
                    "subject_ref": {
                        "authority_domain": "crm.test",
                        "type": "account",
                        "id": "acct-1",
                    },
                    "parameter_constraints": {
                        "account_id": "acct-1",
                        "note": "call tomorrow",
                    },
                    "max_confirmed_occurrences": 1,
                }
            ],
            "approval_requirements": [],
            "completion_contract_ref": {
                "contract_id": "completion:crm-note",
                "contract_version": "1",
            },
            "limits": {
                "max_attempts": 1,
                "task_deadline": "2099-01-01T00:00:00Z",
                "max_executions_per_attempt": 3,
                "max_feedback_cycles": 0,
                "max_reconcile_cycles": 1,
            },
        }
    )


def _config(path: Path) -> ProductionConfig:
    return ProductionConfig(
        database_path=path,
        lease_duration_seconds=0.1,
        heartbeat_interval_seconds=0.05,
    )


def _invoke(app: ProductionRuntime, catalog: CatalogTool, *, execution_id: str):
    submission = app.submit_task(
        command_id=f"submit:{execution_id}",
        task_id=f"task:{execution_id}",
        contract=_contract(catalog),
    )
    claim = app.runtime.claim_and_start_execution(
        lease_owner_id=f"worker:{execution_id}",
        lease_duration_seconds=0.1,
        execution_id=execution_id,
    )
    assert claim is not None
    app.runtime.begin_execution(
        execution_id=claim.execution_id,
        task_id=claim.task_id,
        attempt_id=claim.attempt_id,
        lease_owner_id=claim.lease_owner_id,
        lease_token=claim.lease_token,
    )
    result = app.gateway.invoke(
        ToolInvocationContext(
            execution_id=claim.execution_id,
            task_id=submission.task_id,
            attempt_id=submission.attempt_id,
            tool_call_id=f"call:{execution_id}",
            allowed_tools=(catalog.tool_name,),
            run_request_id=claim.run_request_id,
            lease_owner_id=claim.lease_owner_id,
            lease_token=claim.lease_token,
        ),
        catalog.tool_name,
        {"account_id": "acct-1", "note": "call tomorrow"},
    )
    return submission, claim, result


def test_gateway_integration_executes_and_persists_crm_extension(tmp_path):
    catalog = _catalog()
    adapter = AccountNoteAdapter()
    register_catalog_tool(catalog, display_name="Set account note")
    try:
        with ProductionRuntime(
            _config(tmp_path / "crm-normal.sqlite3"),
            domain_extensions=(_extension(adapter, catalog),),
            executor_factory=lambda _: NoopExecutor(),
        ) as app:
            submission, claim, result = _invoke(app, catalog, execution_id="crm-normal")
            assert result.ok is True
            assert adapter.confirmation_count == 1
            assert app.store.query_one(
                "SELECT status FROM phase3_external_operations WHERE task_id = ?",
                (submission.task_id,),
            )["status"] == "confirmed"
            assert app.store.query_one(
                "SELECT receipt_id FROM execution_receipts WHERE task_id = ?",
                (submission.task_id,),
            )["receipt_id"] == result.receipt_id
            observation = app.store.query_one(
                "SELECT observation_json FROM phase3_business_observations WHERE task_id = ?",
                (submission.task_id,),
            )
            assert '"external_object_id":"note-1"' in observation["observation_json"]
            adapter.notes["note-1"]["body"] = "changed outside Astra"
            status = app.task_status(submission.task_id)
            assert status["observations"][0]["observation_json"]["payload"] == {
                "account": "acct-1",
                "body": "call tomorrow",
            }
            assert status["business_objects"] == [
                {"account": "acct-1", "body": "call tomorrow"}
            ]
            invocation = app.runtime.build_runtime_invocation(claim)
            validation = app.validator.validate(
                invocation,
                {"note_id": "note-1"},
                [result.receipt_id],
                [result.receipt_id],
            )
            assert validation.valid is True
            assert "ticket_id" not in validation.checks
    finally:
        unregister_catalog_tool(catalog.tool_name)


def test_crm_adapter_reconciles_response_loss_after_runtime_reopen(tmp_path):
    catalog = _catalog()
    adapter = AccountNoteAdapter()
    adapter.response_lost_once = True
    extension = _extension(adapter, catalog)
    database = tmp_path / "crm-recovery.sqlite3"
    register_catalog_tool(catalog, display_name="Set account note")
    try:
        with ProductionRuntime(
            _config(database),
            domain_extensions=(extension,),
            executor_factory=lambda _: NoopExecutor(),
        ) as app:
            submission, _, result = _invoke(app, catalog, execution_id="crm-lost")
            assert result.ok is False
            assert result.error["type"] == "external_operation_indeterminate"
            assert len(adapter.notes) == 1
        time.sleep(0.15)
        with ProductionRuntime(
            _config(database),
            domain_extensions=(extension,),
            executor_factory=lambda _: NoopExecutor(),
        ) as app:
            operation = app.store.query_one(
                "SELECT status, operation_json FROM phase3_external_operations WHERE task_id = ?",
                (submission.task_id,),
            )
            assert operation["status"] == "confirmed"
            assert adapter.confirmation_count == 1
            assert app.store.query_one(
                "SELECT COUNT(*) FROM phase3_business_observations WHERE task_id = ?",
                (submission.task_id,),
            )[0] == 1
            assert len(adapter.notes) == 1
    finally:
        unregister_catalog_tool(catalog.tool_name)


def test_crm_and_complaint_result_evaluators_are_disjoint(tmp_path):
    store_path = tmp_path / "evaluator-business.sqlite3"
    from astra.storage import AstraStore

    store = AstraStore(store_path)
    try:
        complaint = ComplaintResultEvaluator(MockBusinessService(store))
        crm = CrmResultEvaluator()
        invocation = RuntimeInvocation(
            execution_id="execution",
            task_id="task",
            attempt_id="attempt",
            user_request="opaque",
            task_contract={},
            allowed_tools=(),
            provider_config={},
        )
        crm_receipts = ({"tool_name": "set_account_note"},)
        complaint_receipts = ({"tool_name": "create_complaint_ticket"},)
        assert crm.handles(invocation, {"note_id": "note-1"}, crm_receipts)
        assert not complaint.handles(invocation, {"note_id": "note-1"}, crm_receipts)
        assert complaint.handles(
            invocation, {"ticket_id": "ticket-1"}, complaint_receipts
        )
        assert not crm.handles(
            invocation, {"ticket_id": "ticket-1"}, complaint_receipts
        )
        registry = ResultEvaluatorRegistry()
        registry.register(crm)
        registry.register(complaint)
        assert registry.registered_refs == (
            ("complaint.result", "1"),
            ("crm.account_note.result", "1"),
        )
    finally:
        store.close()


def test_missing_domain_evaluator_keeps_platform_checks_but_fails_closed(tmp_path):
    catalog = _catalog()
    adapter = AccountNoteAdapter()
    extension = _extension(adapter, catalog)
    extension_without_evaluator = DomainExtension(
        extension_id=extension.extension_id,
        tool_definitions=extension.tool_definitions,
    )
    register_catalog_tool(catalog, display_name="Set account note")
    try:
        with ProductionRuntime(
            _config(tmp_path / "crm-no-evaluator.sqlite3"),
            domain_extensions=(extension_without_evaluator,),
            executor_factory=lambda _: NoopExecutor(),
        ) as app:
            _, claim, result = _invoke(app, catalog, execution_id="crm-no-evaluator")
            validation = app.validator.validate(
                app.runtime.build_runtime_invocation(claim),
                {"note_id": "note-1"},
                [result.receipt_id],
                [result.receipt_id],
            )
            assert validation.valid is False
            assert validation.errors == ("domain_result_evaluator_missing",)
            assert validation.checks["receipt_refs_resolvable"] is True
            assert validation.checks["receipt_refs_owned_by_task"] is True
            assert validation.checks["receipt_refs_in_evidence_grant"] is True
            assert validation.checks["domain_result_evaluated"] is False
    finally:
        unregister_catalog_tool(catalog.tool_name)


def test_generic_execution_files_have_no_complaint_field_branches():
    root = Path(__file__).parents[1] / "astra"
    for name in ("runtime.py", "result_validator.py", "production.py"):
        source = (root / name).read_text(encoding="utf-8")
        for forbidden in (
            "create_complaint_ticket",
            "get_complaint_ticket",
            "ticket_id",
            "customer_id",
            "order_id",
        ):
            assert forbidden not in source
