from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from astra.cli import main
from astra.domain import (
    ExecutionStatus,
)
from astra.hermes_adapter.executor import HermesExecutor
from astra.mock_business import MockBusinessService
from astra.phase3.approval import (
    ApprovalDecision,
    ApprovalRequest,
    ApprovalResolution,
)
from astra.phase3.completion import (
    CompletionContract,
    CompletionRequirement,
    EvaluatorRef,
)
from astra.phase3.effects import EffectContractError
from astra.phase3.policy import PolicyAction, TaskState
from astra.phase3.task_contract import TaskContract
from astra.production import (
    ProductionConfig,
    ProductionRuntime,
)
from astra.runtime import CommandIdentityConflict
from astra.tool_gateway import production_tool_schema_hash


def _contract(*, limits: dict | None = None) -> TaskContract:
    return TaskContract.materialize(
        {
            "schema_version": "1",
            "contract_id": "contract-production-black-box",
            "contract_version": "1",
            "task_type": "production_black_box",
            "execution_type": "agent_executor_port",
            "objective": {
                "description": "Wait for operator input, then finish the task."
            },
            "subject_refs": [],
            "input_snapshot": {
                "schema_id": "production.black_box.input",
                "schema_version": "1",
                "values": {},
                "content_hash": "sha256:production-black-box-input",
            },
            "allowed_capabilities": [],
            "resolved_tools": [],
            "constraints": [],
            "authorized_effects": [],
            "approval_requirements": [],
            "completion_contract_ref": {
                "contract_id": "completion-production-black-box",
                "contract_version": "1",
            },
            "limits": limits
            or {
                "max_attempts": 1,
                "task_deadline": "2099-01-01T00:00:00Z",
                "max_executions_per_attempt": 3,
                "max_feedback_cycles": 1,
                "max_reconcile_cycles": 0,
            },
        }
    )


def _completion_contract() -> CompletionContract:
    return CompletionContract(
        contract_id="completion-production-black-box",
        contract_version="1",
        task_type="production_black_box",
        requirements=(),
    )


def _config(database: Path) -> ProductionConfig:
    return ProductionConfig(
        database_path=database,
        hermes_root=Path(__file__).parents[1] / "hermes-agent-main",
        provider_config={
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "production-dummy-key",
            "provider": "custom",
            "api_mode": "chat_completions",
            "model": "astra-production-scripted-provider",
        },
        worker_poll_interval=0.01,
        hermes_session_database_path=database.with_suffix(".hermes.sqlite3"),
    )


def _effect_contract(
    *,
    schema_hash: str | None = None,
    capability_ref: str = "complaints.integration@1",
    tool_version: str = "1",
    access_mode: str = "effect",
    constraints=(),
    parameter_constraints=None,
    approval_required: bool = False,
    contract_subject_id: str = "order-001",
    limits: dict | None = None,
) -> TaskContract:
    approval_ref = "complaint.approval@1" if approval_required else None
    return TaskContract.materialize(
        {
            "schema_version": "1",
            "contract_id": "contract-production-effect",
            "contract_version": "1",
            "task_type": "production_effect",
            "execution_type": "tool_execution",
            "objective": {"description": "Create the governed complaint ticket."},
            "subject_refs": [
                {
                    "authority_domain": "commerce.mock",
                    "type": "order",
                    "id": contract_subject_id,
                }
            ],
            "input_snapshot": {
                "schema_id": "production.effect.input",
                "schema_version": "1",
                "values": {"order_id": "order-001"},
                "content_hash": "sha256:production-effect-input",
            },
            "allowed_capabilities": [
                {
                    "capability_id": capability_ref.split("@", 1)[0],
                    "capability_version": capability_ref.split("@", 1)[1],
                }
            ],
            "resolved_tools": [
                {
                    "tool_name": "create_complaint_ticket",
                    "tool_version": tool_version,
                    "schema_hash": schema_hash
                    or production_tool_schema_hash("create_complaint_ticket"),
                    "capability_ref": capability_ref,
                    "access_mode": access_mode,
                }
            ],
            "constraints": list(constraints),
            "authorized_effects": [
                {
                    "effect_intent_id": "create-production-complaint",
                    "effect_type": "support.complaint_ticket",
                    "effect_type_version": "1",
                    "authority_domain": "support.mock",
                    "subject_ref": {
                        "authority_domain": "commerce.mock",
                        "type": "order",
                        "id": "order-001",
                    },
                    "parameter_constraints": parameter_constraints or {},
                    "max_confirmed_occurrences": 1,
                    **(
                        {"approval_requirement_ref": approval_ref}
                        if approval_ref is not None
                        else {}
                    ),
                }
            ],
            "approval_requirements": (
                [
                    {
                        "approval_requirement_id": "complaint.approval",
                        "approval_requirement_version": "1",
                        "effect_intent_refs": ["create-production-complaint"],
                        "risk_class": "high",
                        "approver_policy_ref": "support.supervisor@1",
                        "usage_semantics": "single_effect_single_use",
                    }
                ]
                if approval_required
                else []
            ),
            "completion_contract_ref": {
                "contract_id": "completion-production-effect",
                "contract_version": "1",
            },
            "limits": limits
            or {
                "max_attempts": 1,
                "task_deadline": "2099-01-01T00:00:00Z",
                "max_executions_per_attempt": 2,
                "max_feedback_cycles": 0,
                "max_reconcile_cycles": 0,
            },
        }
    )


def _effect_completion_contract(
    *, require_confirmed_effect: bool = False
) -> CompletionContract:
    requirements = ()
    if require_confirmed_effect:
        requirements = (
            CompletionRequirement(
                requirement_id="confirmed_effect",
                description="A governed effect must be authoritatively confirmed.",
                evaluator=EvaluatorRef(
                    evaluator_id="astra.authorized_effect_confirmed",
                    evaluator_version="1",
                ),
                configuration={
                    "effect_intent_ref": "create-production-complaint",
                    "authority_domain": "support.mock",
                    "effect_type": "support.complaint_ticket",
                    "effect_type_version": "1",
                },
                required_evidence=("external_operation", "business_state"),
            ),
        )
    return CompletionContract(
        contract_id="completion-production-effect",
        contract_version="1",
        task_type="production_effect",
        requirements=requirements,
    )


def _two_effect_contract() -> TaskContract:
    payload = _effect_contract(
        parameter_constraints={"resolution": "open governed complaint"}
    ).model_dump(mode="json", exclude_none=True)
    payload.pop("contract_hash", None)
    payload["authorized_effects"].append(
        {
            "effect_intent_id": "create-second-production-complaint",
            "effect_type": "support.complaint_ticket",
            "effect_type_version": "1",
            "authority_domain": "support.mock",
            "subject_ref": {
                "authority_domain": "commerce.mock",
                "type": "order",
                "id": "order-001",
            },
            "parameter_constraints": {
                "resolution": "open second governed complaint"
            },
            "max_confirmed_occurrences": 1,
        }
    )
    return TaskContract.materialize(payload)


def _two_effect_completion_contract() -> CompletionContract:
    requirements = tuple(
        CompletionRequirement(
            requirement_id=requirement_id,
            description=description,
            evaluator=EvaluatorRef(
                evaluator_id="astra.authorized_effect_confirmed",
                evaluator_version="1",
            ),
            configuration={"effect_intent_ref": effect_intent_ref},
            required_evidence=("external_operation", "business_state"),
        )
        for requirement_id, description, effect_intent_ref in (
            (
                "first_effect_confirmed",
                "The first governed effect is confirmed.",
                "create-production-complaint",
            ),
            (
                "second_effect_confirmed",
                "The second governed effect is independently confirmed.",
                "create-second-production-complaint",
            ),
        )
    )
    return CompletionContract(
        contract_id="completion-production-effect",
        contract_version="1",
        task_type="production_effect",
        requirements=requirements,
    )


def _approved_resolution(request: ApprovalRequest) -> ApprovalResolution:
    valid_from = datetime.now(timezone.utc) - timedelta(seconds=1)
    return ApprovalResolution(
        approval_resolution_id="approval-resolution:" + request.approval_request_id,
        approval_request_id=request.approval_request_id,
        interaction_id=request.interaction_id,
        decision=ApprovalDecision.APPROVED,
        task_contract_ref=request.task_contract_ref,
        effect_identity=request.effect_identity,
        effect_request_hash=request.effect_request_hash,
        approval_requirement_ref=request.approval_requirement_ref,
        approver_subject_ref="operator:support-supervisor",
        approver_policy_ref=request.approver_policy_ref,
        permission_scope=request.permission_scope,
        resolved_at=datetime.now(timezone.utc),
        valid_from=valid_from,
        usage_semantics="single_effect_single_use",
    )


def _tool_call(call_id: str, name: str, arguments: dict):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def _provider_response(*, content="", finish_reason="stop", tool_calls=None):
    return SimpleNamespace(
        id="production-scripted-response",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        ],
        model="astra-production-scripted-provider",
        usage=SimpleNamespace(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
        ),
    )


class WaitingThenTerminalProvider:
    """Script only the provider boundary; Hermes owns both execution turns."""

    def __init__(self) -> None:
        self.invocations = []
        self.requests = []

    def client_for(self, invocation):
        self.invocations.append(invocation)
        client = MagicMock()
        client.chat.completions.create.side_effect = self._create
        return client

    def _create(self, **request):
        self.requests.append(request)
        feedback = "\n".join(
            str(message.get("content", ""))
            for message in request.get("messages", ())
            if message.get("role") == "system"
        )
        tool_results = [
            json.loads(str(message.get("content", "{}")))
            for message in request.get("messages", ())
            if message.get("role") == "tool"
        ]
        if "InteractionResolution" not in feedback:
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-production-input",
                        "request_user_input",
                        {
                            "prompt": "Provide the release confirmation.",
                            "details": {"field": "confirmation"},
                        },
                    )
                ],
            )
        if not any(result.get("valid") is not None for result in tool_results):
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-production-input-submit",
                        "submit_task_result",
                        {
                            "outcome": {"confirmed": True},
                            "evidence_refs": [],
                            "receipt_refs": [],
                        },
                    )
                ],
            )
        return _provider_response(content="Input was applied by the resumed Hermes turn.")


class PolicyInputProvider:
    """Finish once without a submission so Governance must request input."""

    def __init__(self) -> None:
        self.invocations = []

    def client_for(self, invocation):
        self.invocations.append(invocation)
        client = MagicMock()
        client.chat.completions.create.side_effect = self._create
        return client

    @staticmethod
    def _create(**request):
        feedback = "\n".join(
            str(message.get("content", ""))
            for message in request.get("messages", ())
            if message.get("role") == "system"
        )
        tool_results = [
            json.loads(str(message.get("content", "{}")))
            for message in request.get("messages", ())
            if message.get("role") == "tool"
        ]
        if "InteractionResolution" not in feedback:
            return _provider_response(content="Required input is still missing.")
        if not any(result.get("valid") is not None for result in tool_results):
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-policy-input-submit",
                        "submit_task_result",
                        {
                            "outcome": {"input_received": True},
                            "evidence_refs": [],
                            "receipt_refs": [],
                        },
                    )
                ],
            )
        return _provider_response(content="Input applied.")


class FailedEffectPolicyProvider:
    """Drive failed governed effects through Hermes and the real Gateway."""

    def __init__(self) -> None:
        self.invocations = []

    def client_for(self, invocation):
        self.invocations.append(invocation)
        invocation_ordinal = len(self.invocations)
        client = MagicMock()

        def create(**request):
            return self._create(invocation, invocation_ordinal, request)

        client.chat.completions.create.side_effect = create
        return client

    @staticmethod
    def _create(invocation, invocation_ordinal, request):
        tool_results = [
            json.loads(str(message.get("content", "{}")))
            for message in request.get("messages", ())
            if message.get("role") == "tool"
        ]
        if invocation_ordinal == 1 and not tool_results:
            return _provider_response(content="Required input is missing.")
        current_effect_results = [
            result
            for result in tool_results
            if result.get("tool_name") == "create_complaint_ticket"
        ]
        if not current_effect_results and not any(
            result.get("valid") is not None for result in tool_results
        ):
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-failed-policy-effect:" + invocation.execution_id,
                        "create_complaint_ticket",
                        {
                            "customer_id": "customer-not-on-order",
                            "order_id": "order-001",
                            "reason": "damaged in transit",
                            "resolution": "open governed complaint",
                        },
                    )
                ],
            )
        if not any(result.get("valid") is not None for result in tool_results):
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-failed-policy-submit:" + invocation.execution_id,
                        "submit_task_result",
                        {
                            "outcome": {"confirmed": False},
                            "evidence_refs": [],
                            "receipt_refs": [],
                        },
                    )
                ],
            )
        return _provider_response(content="The governed effect was not confirmed.")


class EffectProviderClient:
    def __init__(
        self,
        *,
        block_first_call: bool = False,
        double_effect: bool = False,
    ) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        if not block_first_call:
            self.release.set()
        self.tool_results = []
        self.double_effect = double_effect
        self.client = MagicMock()
        self.client.chat.completions.create.side_effect = self._create

    def _create(self, **request):
        results = [
            json.loads(str(message.get("content", "{}")))
            for message in request.get("messages", ())
            if message.get("role") == "tool"
        ]
        self.tool_results = results
        if not results:
            self.started.set()
            if not self.release.wait(timeout=10):
                raise RuntimeError("scripted provider release timeout")
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-production-effect",
                        "create_complaint_ticket",
                        {
                            "customer_id": "cust-001",
                            "order_id": "order-001",
                            "reason": "damaged in transit",
                            "resolution": "open governed complaint",
                        },
                    )
                ],
            )
        latest = results[-1]
        if (latest.get("error") or {}).get("type") == "approval_required":
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-production-effect-after-approval",
                        "create_complaint_ticket",
                        {
                            "customer_id": "cust-001",
                            "order_id": "order-001",
                            "reason": "damaged in transit",
                            "resolution": "open governed complaint",
                        },
                    )
                ],
            )
        if latest.get("ok") is True and latest.get("receipt_id"):
            effect_results = [
                result
                for result in results
                if result.get("tool_name") == "create_complaint_ticket"
            ]
            if self.double_effect and len(effect_results) == 1:
                return _provider_response(
                    finish_reason="tool_calls",
                    tool_calls=[
                        _tool_call(
                            "call-production-effect-second",
                            "create_complaint_ticket",
                            {
                                "customer_id": "cust-001",
                                "order_id": "order-001",
                                "reason": "second independent reason",
                                "resolution": "open governed complaint",
                            },
                        )
                    ],
                )
            ticket_id = latest["data"]["ticket"]["ticket_id"]
            receipt_id = latest["receipt_id"]
            if not any(result.get("valid") is not None for result in results):
                return _provider_response(
                    finish_reason="tool_calls",
                    tool_calls=[
                        _tool_call(
                            "call-production-submit",
                            "submit_task_result",
                            {
                                "outcome": {
                                    "customer_id": "cust-001",
                                    "order_id": "order-001",
                                    "ticket_id": ticket_id,
                                },
                                "evidence_refs": [receipt_id],
                                "receipt_refs": [receipt_id],
                            },
                        )
                    ],
                )
        return _provider_response(content="Production effect turn finished.")


class MissingConfirmationBusinessService(MockBusinessService):
    """External adapter fault: creation is acknowledged but read-back is absent."""

    def __init__(self, store) -> None:
        super().__init__(store)
        self.hide_readback = False

    def create_complaint_ticket(self, **kwargs):
        ticket, created = super().create_complaint_ticket(**kwargs)
        self.hide_readback = True
        return ticket, created

    def get_complaint_ticket(self, ticket_id):
        if self.hide_readback:
            return None
        return super().get_complaint_ticket(ticket_id)


class SubmitOnlyProviderClient:
    def __init__(self) -> None:
        self.client = MagicMock()
        self.client.chat.completions.create.side_effect = self._create

    @staticmethod
    def _create(**request):
        results = [
            json.loads(str(message.get("content", "{}")))
            for message in request.get("messages", ())
            if message.get("role") == "tool"
        ]
        if not results:
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-submit-only",
                        "submit_task_result",
                        {
                            "outcome": {"completed": True},
                            "evidence_refs": [],
                            "receipt_refs": [],
                        },
                    )
                ],
            )
        return _provider_response(content="Submission recorded.")


class ProactiveApprovalProviderClient:
    def __init__(self) -> None:
        self.client = MagicMock()
        self.client.chat.completions.create.side_effect = self._create

    def _create(self, **request):
        messages = request.get("messages", ())
        results = [
            json.loads(str(message.get("content", "{}")))
            for message in messages
            if message.get("role") == "tool"
        ]
        runtime_feedback = "\n".join(
            str(message.get("content", ""))
            for message in messages
            if message.get("role") == "system"
        )
        effect_arguments = {
            "customer_id": "cust-001",
            "order_id": "order-001",
            "reason": "damaged in transit",
            "resolution": "open governed complaint",
        }
        if not results:
            if "InteractionResolution" in runtime_feedback:
                return _provider_response(
                    finish_reason="tool_calls",
                    tool_calls=[
                        _tool_call(
                            "call-proactive-effect",
                            "create_complaint_ticket",
                            effect_arguments,
                        )
                    ],
                )
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-proactive-approval",
                        "request_approval",
                        {
                            "prompt": "Approve the exact complaint ticket effect.",
                            "tool_name": "create_complaint_ticket",
                            "arguments": effect_arguments,
                        },
                    )
                ],
            )
        latest = results[-1]
        if latest.get("ok") is True and latest.get("receipt_id"):
            receipt_id = latest["receipt_id"]
            ticket_id = latest["data"]["ticket"]["ticket_id"]
            return _provider_response(
                finish_reason="tool_calls",
                tool_calls=[
                    _tool_call(
                        "call-proactive-submit",
                        "submit_task_result",
                        {
                            "outcome": {"ticket_id": ticket_id},
                            "evidence_refs": [receipt_id],
                            "receipt_refs": [receipt_id],
                        },
                    )
                ],
            )
        return _provider_response(content="Proactive approval turn finished.")

def test_default_production_composition_has_one_runtime_authority(tmp_path):
    with ProductionRuntime(_config(tmp_path / "composition.sqlite3")) as app:
        assert isinstance(app.executor, HermesExecutor)
        assert app.runtime is app.worker.runtime
        assert app.runtime is app.executor.runtime
        assert app.store is app.runtime.store
        assert app.store is app.runtime.governance_store.authority_store
        assert app.store.connection is app.runtime.governance_store.connection
        identity = app.authority_identity
        assert identity["runtime"] == identity["worker_runtime"]
        assert identity["runtime"] == identity["executor_runtime"]


def test_formal_entry_points_complete_persisted_hermes_black_box(tmp_path):
    database = tmp_path / "black-box.sqlite3"
    provider = WaitingThenTerminalProvider()
    with ProductionRuntime(
        _config(database), hermes_client_factory=provider.client_for
    ) as app:
        submission = app.submit_task(
            command_id="submit:production-black-box",
            task_id="task:production-black-box",
            contract=_contract(),
            completion_contract=_completion_contract(),
        )
        first = asyncio.run(app.run_worker_once())
        assert first is not None
        assert isinstance(app.executor, HermesExecutor)
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        assert interaction is not None
        first_execution = app.store.get_execution(first.claim.execution_id)
        assert first_execution["status"] == "waiting_input"
        assert first_execution["session_handle"]
        waiting_status = app.task_status(submission.task_id)
        assert waiting_status["task"]["state"] == "waiting_input"
        assert waiting_status["interactions"][0]["status"] == "pending"
        persisted_session = str(first_execution["session_handle"])
        interaction_id = str(interaction["interaction_id"])
        interaction_version = int(interaction["version"])

    # Reopen the entire Production composition before resolution. The resumed
    # Hermes turn can only receive session and feedback from durable records.
    with ProductionRuntime(
        _config(database), hermes_client_factory=provider.client_for
    ) as app:
        resolved = app.resolve_interaction(
            command_id="resolve:production-black-box",
            interaction_id=interaction_id,
            expected_version=interaction_version,
            resolution={"confirmation": "approved"},
        )
        resumed_request = app.store.query_one(
            "SELECT * FROM phase4_run_requests WHERE run_request_id = ?",
            (resolved.run_request_id,),
        )
        assert resumed_request["session_handle"] == persisted_session
        assert json.loads(resumed_request["feedback_json"]) == [
            {
                "type": "InteractionResolution",
                "interaction_id": interaction_id,
                "kind": "user_input",
                "resolution": {"confirmation": "approved"},
            }
        ]

        second = asyncio.run(app.run_worker_once())
        assert second is not None
        task = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        assert task["state"] == "succeeded"
        assert second.claim.attempt_id == submission.attempt_id
        assert second.invocation.session_handle == persisted_session
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_attempts WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM executions WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 2
        assert app.store.query_one(
            "SELECT COUNT(*) FROM interactions WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase4_run_requests WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 2
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase4_run_requests WHERE source_interaction_id = ?",
            (interaction_id,),
        )[0] == 1
        completed_status = app.task_status(submission.task_id)
        assert completed_status["task"]["state"] == "succeeded"
        assert completed_status["interactions"][0]["status"] == "resolved"
        assert len(completed_status["rule_evaluations"]) == 5


def test_submit_and_resolve_commands_use_the_production_composition(
    tmp_path, capsys
):
    database = tmp_path / "commands.sqlite3"
    contract_path = tmp_path / "task-contract.json"
    completion_path = tmp_path / "completion-contract.json"
    contract_path.write_text(_contract().model_dump_json())
    completion_path.write_text(_completion_contract().model_dump_json())

    assert main(
        [
            "--database",
            str(database),
            "submit",
            "--command-id",
            "submit:production-command",
            "--task-id",
            "task:production-command",
            "--contract",
            str(contract_path),
            "--completion-contract",
            str(completion_path),
        ]
    ) == 0
    assert "task:production-command" in capsys.readouterr().out

    provider = WaitingThenTerminalProvider()
    with ProductionRuntime(
        _config(database), hermes_client_factory=provider.client_for
    ) as app:
        first = asyncio.run(app.run_worker_once())
        assert first is not None
        interaction = app.store.query_one(
            "SELECT interaction_id, version FROM interactions WHERE task_id = ?",
            ("task:production-command",),
        )
        assert interaction is not None
        interaction_id = str(interaction["interaction_id"])
        interaction_version = int(interaction["version"])

    assert main(
        [
            "--database",
            str(database),
            "resolve",
            "--command-id",
            "resolve:production-command",
            "--interaction-id",
            interaction_id,
            "--expected-version",
            str(interaction_version),
            "--resolution-json",
            '{"confirmation":"approved"}',
        ]
    ) == 0
    assert '"run_request_state":"pending"' in capsys.readouterr().out

    with ProductionRuntime(_config(database)) as app:
        assert app.store.query_one(
            """
            SELECT COUNT(*) FROM phase4_run_requests
            WHERE task_id = ? AND source_interaction_id = ? AND state = 'pending'
            """,
            ("task:production-command", interaction_id),
        )[0] == 1


def test_policy_request_input_uses_runtime_wait_and_formal_resolve(tmp_path):
    provider = PolicyInputProvider()
    with ProductionRuntime(
        _config(tmp_path / "policy-input.sqlite3"),
        hermes_client_factory=provider.client_for,
    ) as app:
        submission = app.submit_task(
            command_id="submit:policy-input",
            task_id="task:policy-input",
            contract=_contract(),
            completion_contract=_completion_contract(),
        )
        first = asyncio.run(app.run_worker_once())
        assert first.governance_evaluation.policy_decision.action == PolicyAction.REQUEST_INPUT
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        decision_id = first.governance_evaluation.policy_decision.decision_id
        assert interaction["created_by_decision_id"] == decision_id
        assert app.store.get_execution(first.claim.execution_id)["ended_at"] is not None
        assert first.run_request_state == "completed"
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] == "waiting_input"

        resolved = app.resolve_interaction(
            command_id="resolve:policy-input",
            interaction_id=str(interaction["interaction_id"]),
            expected_version=int(interaction["version"]),
            resolution={"required_value": "provided"},
        )
        second = asyncio.run(app.run_worker_once())
        assert resolved.attempt_id == submission.attempt_id
        assert second.claim.attempt_id == submission.attempt_id
        assert second.invocation.feedback[0]["type"] == "InteractionResolution"
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] == "succeeded"


def test_pending_policy_decision_is_applied_once_by_startup_recovery(tmp_path):
    database = tmp_path / "policy-recovery.sqlite3"
    provider = PolicyInputProvider()

    def crash_after_decision(point, payload):
        assert point == "after_policy_decision_persisted"
        assert payload["decision_id"]
        raise RuntimeError("injected_process_loss_after_policy_decision")

    with ProductionRuntime(
        _config(database),
        hermes_client_factory=provider.client_for,
        worker_fault_injector=crash_after_decision,
    ) as app:
        submission = app.submit_task(
            command_id="submit:policy-recovery",
            task_id="task:policy-recovery",
            contract=_contract(),
            completion_contract=_completion_contract(),
        )
        with pytest.raises(
            RuntimeError, match="injected_process_loss_after_policy_decision"
        ):
            asyncio.run(app.run_worker_once())
        decision = app.store.query_one(
            "SELECT * FROM phase3_policy_decisions WHERE task_id = ?",
            (submission.task_id,),
        )
        assert decision["application_status"] == "pending"
        decision_id = str(decision["decision_id"])
        snapshot_id = str(
            app.store.query_one(
                "SELECT evidence_snapshot_id FROM phase3_evidence_snapshots WHERE task_id = ?",
                (submission.task_id,),
            )["evidence_snapshot_id"]
        )

    with ProductionRuntime(
        _config(database), hermes_client_factory=provider.client_for
    ) as app:
        assert len(app.startup_recovery) == 1
        assert app.startup_recovery[0].decision_id == decision_id
        assert app.startup_recovery[0].application.value == "applied"
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] == "waiting_input"
        assert app.store.query_one(
            "SELECT COUNT(*) FROM interactions WHERE created_by_decision_id = ?",
            (decision_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_policy_decisions WHERE decision_key = ?",
            (decision["decision_key"],),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_evidence_snapshots WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT evidence_snapshot_id FROM phase3_evidence_snapshots WHERE task_id = ?",
            (submission.task_id,),
        )["evidence_snapshot_id"] == snapshot_id

    with ProductionRuntime(
        _config(database), hermes_client_factory=provider.client_for
    ) as app:
        assert app.startup_recovery == ()
        assert app.store.query_one(
            "SELECT COUNT(*) FROM interactions WHERE created_by_decision_id = ?",
            (decision_id,),
        )[0] == 1


def test_interaction_resolve_enforces_max_executions_without_new_work(tmp_path):
    limits = {
        "max_attempts": 1,
        "task_deadline": "2099-01-01T00:00:00Z",
        "max_executions_per_attempt": 1,
        "max_feedback_cycles": 1,
        "max_reconcile_cycles": 0,
    }
    provider = PolicyInputProvider()
    with ProductionRuntime(
        _config(tmp_path / "interaction-execution-limit.sqlite3"),
        hermes_client_factory=provider.client_for,
    ) as app:
        submission = app.submit_task(
            command_id="submit:interaction-execution-limit",
            task_id="task:interaction-execution-limit",
            contract=_contract(limits=limits),
            completion_contract=_completion_contract(),
        )
        waiting = asyncio.run(app.run_worker_once())
        assert waiting.governance_evaluation.policy_decision.action == PolicyAction.REQUEST_INPUT
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        resolved = app.resolve_interaction(
            command_id="resolve:interaction-execution-limit",
            interaction_id=str(interaction["interaction_id"]),
            expected_version=int(interaction["version"]),
            resolution={"required_value": "provided"},
        )
        assert resolved.run_request_id is None
        assert resolved.task_state == "failed"
        assert app.store.query_one(
            "SELECT termination_reason FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["termination_reason"] == "attempt_budget_exhausted"
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase4_run_requests WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1


def test_policy_continue_with_feedback_is_exactly_once_and_bounded(tmp_path):
    limits = {
        "max_attempts": 1,
        "task_deadline": "2099-01-01T00:00:00Z",
        "max_executions_per_attempt": 3,
        "max_feedback_cycles": 1,
        "max_reconcile_cycles": 1,
    }
    provider = FailedEffectPolicyProvider()
    with ProductionRuntime(
        _config(tmp_path / "policy-continue.sqlite3"),
        hermes_client_factory=provider.client_for,
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:policy-continue",
            task_id="task:policy-continue",
            contract=_effect_contract(limits=limits),
            completion_contract=_effect_completion_contract(
                require_confirmed_effect=True
            ),
        )
        waiting = asyncio.run(app.run_worker_once())
        assert waiting.governance_evaluation.policy_decision.action == PolicyAction.REQUEST_INPUT
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        app.resolve_interaction(
            command_id="resolve:policy-continue",
            interaction_id=str(interaction["interaction_id"]),
            expected_version=int(interaction["version"]),
            resolution={"continue": True},
        )

        first = asyncio.run(app.run_worker_once())
        decision = first.governance_evaluation.policy_decision
        assert decision.action == PolicyAction.CONTINUE_WITH_FEEDBACK
        continuation = app.store.query_one(
            """
            SELECT * FROM phase4_run_requests
            WHERE created_by_decision_id = ?
            """,
            (decision.decision_id,),
        )
        assert continuation["attempt_id"] == submission.attempt_id
        assert continuation["reason"] == "continue_with_feedback"
        assert continuation["session_handle"] is not None
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase4_run_requests WHERE created_by_decision_id = ?",
            (decision.decision_id,),
        )[0] == 1

        second = asyncio.run(app.run_worker_once())
        assert second.claim.attempt_id == submission.attempt_id
        assert second.invocation.feedback[0]["type"] == "PolicyFeedback"
        assert second.governance_evaluation.policy_decision.action == PolicyAction.FAIL
        assert second.governance_evaluation.policy_decision.reason_code == "attempt_budget_exhausted"
        assert app.store.query_one(
            "SELECT state, termination_reason FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["termination_reason"] == "attempt_budget_exhausted"
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase4_run_requests WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 3


def test_policy_start_new_attempt_is_atomic_and_bounded(tmp_path):
    limits = {
        "max_attempts": 2,
        "task_deadline": "2099-01-01T00:00:00Z",
        "max_executions_per_attempt": 3,
        "max_feedback_cycles": 0,
        "max_reconcile_cycles": 1,
    }
    provider = FailedEffectPolicyProvider()
    with ProductionRuntime(
        _config(tmp_path / "policy-new-attempt.sqlite3"),
        hermes_client_factory=provider.client_for,
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:policy-new-attempt",
            task_id="task:policy-new-attempt",
            contract=_effect_contract(limits=limits),
            completion_contract=_effect_completion_contract(
                require_confirmed_effect=True
            ),
        )
        waiting = asyncio.run(app.run_worker_once())
        assert waiting.governance_evaluation.policy_decision.action == PolicyAction.REQUEST_INPUT
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        app.resolve_interaction(
            command_id="resolve:policy-new-attempt",
            interaction_id=str(interaction["interaction_id"]),
            expected_version=int(interaction["version"]),
            resolution={"continue": True},
        )

        first = asyncio.run(app.run_worker_once())
        decision = first.governance_evaluation.policy_decision
        assert decision.action == PolicyAction.START_NEW_ATTEMPT
        attempts = app.store.query_all(
            "SELECT * FROM phase3_attempts WHERE task_id = ? ORDER BY ordinal",
            (submission.task_id,),
        )
        assert [(row["ordinal"], row["state"]) for row in attempts] == [
            (1, "superseded"),
            (2, "active"),
        ]
        new_attempt = attempts[1]
        request = app.store.query_one(
            "SELECT * FROM phase4_run_requests WHERE created_by_decision_id = ?",
            (decision.decision_id,),
        )
        assert new_attempt["created_by_decision_id"] == decision.decision_id
        assert request["attempt_id"] == new_attempt["attempt_id"]
        assert request["reason"] == "new_attempt"
        assert request["session_handle"] is None

        second = asyncio.run(app.run_worker_once())
        assert second.claim.attempt_id == new_attempt["attempt_id"]
        assert second.governance_evaluation.policy_decision.action == PolicyAction.FAIL
        assert second.governance_evaluation.policy_decision.reason_code == "attempt_budget_exhausted"
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_attempts WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 2
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase4_run_requests WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 3


def test_production_gateway_executes_the_canonical_effect_chain(tmp_path):
    client = EffectProviderClient()
    with ProductionRuntime(
        _config(tmp_path / "canonical-effect.sqlite3"),
        hermes_client_factory=lambda _: client.client,
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:production-effect",
            task_id="task:production-effect",
            contract=_effect_contract(),
            completion_contract=_effect_completion_contract(
                require_confirmed_effect=True
            ),
        )

        run = asyncio.run(app.run_worker_once())

        assert run is not None
        assert run.claim.task_id == submission.task_id
        assert run.execution_finalized is True
        assert run.run_request_state == "completed"
        task = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        assert task["state"] == "succeeded"
        canonical = app.store.query_one(
            "SELECT * FROM phase3_canonical_effect_requests WHERE task_id = ?",
            (submission.task_id,),
        )
        operation = app.store.query_one(
            "SELECT * FROM phase3_external_operations WHERE task_id = ?",
            (submission.task_id,),
        )
        receipt = app.store.query_one(
            "SELECT * FROM execution_receipts WHERE task_id = ? AND side_effect = 1",
            (submission.task_id,),
        )
        tickets = app.business.snapshot()["complaint_tickets"]
        assert canonical is not None
        assert operation is not None and operation["status"] == "confirmed"
        assert receipt is not None
        assert receipt["operation_id"] == operation["operation_id"]
        assert len(tickets) == 1
        assert tickets[0]["idempotency_key"] == receipt["idempotency_key"]
        assert tickets[0]["idempotency_key"].startswith("sha256:")
        assert client.tool_results[-1]["valid"] is True
        assert run.governance_evaluation.completion_validation.status.value == (
            "satisfied"
        )
        assert len(run.governance_evaluation.requirement_evaluations) == 1
        assert len(run.governance_evaluation.rule_evaluations) == 5
        assert {
            evaluation.rule_evaluation_id
            for evaluation in run.governance_evaluation.rule_evaluations
        }.issubset(set(run.governance_evaluation.policy_decision.evaluation_refs))
        assert next(
            evaluation
            for evaluation in run.governance_evaluation.rule_evaluations
            if evaluation.rule_id == "astra.duplicate_side_effect"
        ).status.value == "pass"
        assert tuple(
            (item["rule_id"], item["rule_version"])
            for item in run.governance_evaluation.evidence_snapshot.authoritative_versions[
                "task_rules"
            ]
        ) == app.task_rule_registry.registered_refs
        assert (
            run.governance_evaluation.requirement_evaluations[0].status.value
            == "satisfied"
        )
        evaluation_refs = set(
            run.governance_evaluation.requirement_evaluations[0].evidence_refs
        )
        assert str(operation["operation_id"]) in evaluation_refs
        observation = app.store.query_one(
            """
            SELECT observation_id FROM phase3_business_observations
            WHERE task_id = ?
            """,
            (submission.task_id,),
        )
        assert str(observation["observation_id"]) in evaluation_refs
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_business_observations WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_evidence_snapshots WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_requirement_evaluations WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_rule_evaluations WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 5
        status = app.task_status(submission.task_id)
        assert [
            fact["fact_type"]
            for fact in status["reliability_facts"]
            if fact["fact_type"]
            in {
                "side_effect_requested",
                "external_operation_dispatched",
                "external_operation_acknowledged",
                "external_operation_confirmed",
            }
        ] == [
            "side_effect_requested",
            "external_operation_dispatched",
            "external_operation_acknowledged",
            "external_operation_confirmed",
        ]


def test_production_effect_snapshot_reuses_across_crash_restart(tmp_path):
    database = tmp_path / "effect-snapshot-restart.sqlite3"
    client = EffectProviderClient()

    def crash_after_decision(point, payload):
        assert point == "after_policy_decision_persisted"
        assert payload["decision_id"]
        raise RuntimeError("injected_effect_snapshot_restart")

    with ProductionRuntime(
        _config(database),
        hermes_client_factory=lambda _: client.client,
        worker_fault_injector=crash_after_decision,
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:effect-snapshot-restart",
            task_id="task:effect-snapshot-restart",
            contract=_effect_contract(),
            completion_contract=_effect_completion_contract(
                require_confirmed_effect=True
            ),
        )
        with pytest.raises(RuntimeError, match="injected_effect_snapshot_restart"):
            asyncio.run(app.run_worker_once())
        snapshot = app.store.query_one(
            "SELECT * FROM phase3_evidence_snapshots WHERE task_id = ?",
            (submission.task_id,),
        )
        snapshot_id = str(snapshot["evidence_snapshot_id"])
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_business_observations WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_requirement_evaluations WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT status FROM phase3_external_operations WHERE task_id = ?",
            (submission.task_id,),
        )["status"] == "confirmed"
        assert len(app.business.snapshot()["complaint_tickets"]) == 1

    with ProductionRuntime(
        _config(database), hermes_client_factory=lambda _: client.client
    ) as app:
        assert len(app.startup_recovery) == 1
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] == "succeeded"
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_evidence_snapshots WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT evidence_snapshot_id FROM phase3_evidence_snapshots WHERE task_id = ?",
            (submission.task_id,),
        )["evidence_snapshot_id"] == snapshot_id
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_requirement_evaluations WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_policy_decisions WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 1


def test_production_lifecycle_version_change_creates_new_snapshot_identity(tmp_path):
    provider = PolicyInputProvider()
    with ProductionRuntime(
        _config(tmp_path / "snapshot-version-change.sqlite3"),
        hermes_client_factory=provider.client_for,
    ) as app:
        submission = app.submit_task(
            command_id="submit:snapshot-version-change",
            task_id="task:snapshot-version-change",
            contract=_contract(),
            completion_contract=_completion_contract(),
        )
        asyncio.run(app.run_worker_once())
        first_snapshot = app.store.query_one(
            "SELECT * FROM phase3_evidence_snapshots WHERE task_id = ?",
            (submission.task_id,),
        )
        first_payload = json.loads(first_snapshot["snapshot_json"])
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        app.resolve_interaction(
            command_id="resolve:snapshot-version-change",
            interaction_id=str(interaction["interaction_id"]),
            expected_version=int(interaction["version"]),
            resolution={"required_value": "provided"},
        )
        asyncio.run(app.run_worker_once())
        snapshots = app.store.query_all(
            "SELECT * FROM phase3_evidence_snapshots WHERE task_id = ? ORDER BY created_at",
            (submission.task_id,),
        )
        assert len(snapshots) == 2
        second_payload = json.loads(snapshots[1]["snapshot_json"])
        assert snapshots[0]["evidence_snapshot_id"] != snapshots[1][
            "evidence_snapshot_id"
        ]
        assert first_payload["authoritative_versions"]["interactions"] == {}
        assert second_payload["authoritative_versions"]["interactions"][
            str(interaction["interaction_id"])
        ] == {"state": "resolved", "version": 2}


def test_missing_authoritative_observation_cannot_confirm_or_complete(tmp_path):
    client = EffectProviderClient()
    with ProductionRuntime(
        _config(tmp_path / "missing-observation.sqlite3"),
        hermes_client_factory=lambda _: client.client,
        business_factory=MissingConfirmationBusinessService,
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:missing-observation",
            task_id="task:missing-observation",
            contract=_effect_contract(),
            completion_contract=_effect_completion_contract(
                require_confirmed_effect=True
            ),
        )
        asyncio.run(app.run_worker_once())
        assert len(app.business.snapshot()["complaint_tickets"]) == 1
        assert app.store.query_one(
            "SELECT status FROM phase3_external_operations WHERE task_id = ?",
            (submission.task_id,),
        )["status"] == "indeterminate"
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_business_observations WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 0
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] != "succeeded"


def test_production_registry_is_static_and_complete(tmp_path):
    client = SubmitOnlyProviderClient()
    with ProductionRuntime(
        _config(tmp_path / "production-registry.sqlite3"),
        hermes_client_factory=lambda _: client.client,
    ) as app:
        assert app.evaluator_registry.registered_refs == (
            ("astra.authorized_effect_confirmed", "1"),
        )
        assert app.normalizer_registry.registered_refs == (
            ("astra.create_complaint_ticket", "1"),
        )
        assert app.constraint_registry.registered_refs == (
            ("astra.exact_parameters", "1"),
            ("complaint.order_scope", "1"),
        )
        assert app.task_rule_registry.registered_refs == (
            ("astra.cross_attempt_progress", "1"),
            ("astra.duplicate_side_effect", "1"),
            ("astra.late_state_affecting_record", "1"),
            ("astra.receipt_business_state_mismatch", "1"),
            ("astra.recovery_divergence", "1"),
        )
        assert app.component_registry_manifest == {
            "evaluators": app.evaluator_registry.registered_refs,
            "normalizers": app.normalizer_registry.registered_refs,
            "constraints": app.constraint_registry.registered_refs,
            "task_rules": app.task_rule_registry.registered_refs,
        }
        assert app.governance.task_rule_registry is app.task_rule_registry
        assert "blocked" not in {state.value for state in TaskState}
        with pytest.raises(EffectContractError, match="Unknown normalizer/version"):
            app.normalizer_registry.normalize(
                _effect_contract(),
                {},
                normalizer_id="vendor.unknown",
                normalizer_version="99",
            )


def test_one_confirmed_effect_cannot_satisfy_two_requirements(tmp_path):
    client = EffectProviderClient()
    with ProductionRuntime(
        _config(tmp_path / "independent-requirements.sqlite3"),
        hermes_client_factory=lambda _: client.client,
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:independent-requirements",
            task_id="task:independent-requirements",
            contract=_two_effect_contract(),
            completion_contract=_two_effect_completion_contract(),
        )
        run = asyncio.run(app.run_worker_once())
        assert run is not None
        statuses = {
            evaluation.requirement_id: evaluation.status.value
            for evaluation in run.governance_evaluation.requirement_evaluations
        }
        assert statuses == {
            "first_effect_confirmed": "satisfied",
            "second_effect_confirmed": "unknown",
        }
        assert run.governance_evaluation.completion_validation.status.value == (
            "indeterminate"
        )
        task = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        assert task["state"] != "succeeded"


def test_unknown_evaluator_cannot_make_production_task_succeed(tmp_path):
    client = SubmitOnlyProviderClient()
    completion = CompletionContract(
        contract_id="completion-production-black-box",
        contract_version="1",
        task_type="production_black_box",
        requirements=(
            CompletionRequirement(
                requirement_id="unknown-production-evaluator",
                description="Unknown evaluators fail closed.",
                evaluator=EvaluatorRef(
                    evaluator_id="vendor.unknown",
                    evaluator_version="99",
                ),
            ),
        ),
    )
    with ProductionRuntime(
        _config(tmp_path / "unknown-evaluator.sqlite3"),
        hermes_client_factory=lambda _: client.client,
    ) as app:
        submission = app.submit_task(
            command_id="submit:unknown-evaluator",
            task_id="task:unknown-evaluator",
            contract=_contract(),
            completion_contract=completion,
        )
        initial = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        assert initial["state"] == "pending"
        assert initial["state"] != "blocked"
        run = asyncio.run(app.run_worker_once())
        assert run is not None
        assert run.governance_evaluation.completion_validation.status.value == (
            "evaluator_error"
        )
        assert run.governance_evaluation.policy_decision.action == PolicyAction.ESCALATE
        task = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        assert task["state"] == "failed"


@pytest.mark.parametrize(
    ("contract", "expected_error"),
    (
        (
            _effect_contract(schema_hash="sha256:stale-schema"),
            "tool_schema_hash_mismatch",
        ),
        (
            _effect_contract(capability_ref="complaints.other@1"),
            "capability_mismatch",
        ),
        (
            _effect_contract(contract_subject_id="order-002"),
            "subject_scope_mismatch",
        ),
        (_effect_contract(tool_version="2"), "tool_version_mismatch"),
        (_effect_contract(access_mode="read"), "tool_access_mode_mismatch"),
        (
            _effect_contract(
                constraints=(
                    {
                        "constraint_id": "unknown.constraint",
                        "constraint_version": "1",
                        "kind": "subject_scope",
                        "enforcement_point": "tool_gateway",
                        "configuration": {},
                    },
                )
            ),
            "unknown_constraint_handler",
        ),
        (
            _effect_contract(
                parameter_constraints={"resolution": "different resolution"}
            ),
            "effect_request_must_match_exactly_one_authorized_effect_intent",
        ),
        (_effect_contract(approval_required=True), "approval_required"),
    ),
)
def test_production_gateway_fails_closed_before_business_side_effect(
    tmp_path,
    contract,
    expected_error,
):
    client = EffectProviderClient()
    with ProductionRuntime(
        _config(tmp_path / f"deny-{expected_error}.sqlite3"),
        hermes_client_factory=lambda _: client.client,
    ) as app:
        app.business.seed_normal_complaint()
        app.submit_task(
            command_id="submit:" + expected_error,
            task_id="task:" + expected_error,
            contract=contract,
            completion_contract=_effect_completion_contract(),
        )

        run = asyncio.run(app.run_worker_once())

        assert run is not None
        if expected_error == "approval_required":
            interaction = app.store.query_one(
                "SELECT * FROM interactions WHERE task_id = ?",
                ("task:" + expected_error,),
            )
            assert run.execution_result.status == ExecutionStatus.WAITING_APPROVAL
            assert interaction["kind"] == "approval"
            assert interaction["status"] == "pending"
        else:
            tool_error = next(
                result["error"]
                for result in client.tool_results
                if result.get("tool_name") == "create_complaint_ticket"
            )
            assert tool_error["type"] == expected_error
        assert app.business.snapshot()["complaint_tickets"] == []
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_external_operations"
        )[0] == 0
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_canonical_effect_requests"
        )[0] == 0
        assert app.store.query_one(
            "SELECT COUNT(*) FROM execution_receipts WHERE side_effect = 1"
        )[0] == 0


def test_gateway_approval_wait_resolves_into_same_attempt_and_executes(tmp_path):
    database = tmp_path / "approval-lifecycle.sqlite3"
    client = EffectProviderClient()
    with ProductionRuntime(
        _config(database), hermes_client_factory=lambda _: client.client
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:approval-lifecycle",
            task_id="task:approval-lifecycle",
            contract=_effect_contract(approval_required=True),
            completion_contract=_effect_completion_contract(),
        )
        first = asyncio.run(app.run_worker_once())
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        approval_row = app.store.query_one(
            "SELECT * FROM phase3_approval_requests WHERE interaction_id = ?",
            (interaction["interaction_id"],),
        )
        first_execution = app.store.get_execution(first.claim.execution_id)
        first_request = app.store.query_one(
            "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
            (submission.run_request_id,),
        )
        task = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )
        attempt = app.store.query_one(
            "SELECT state FROM phase3_attempts WHERE attempt_id = ?",
            (submission.attempt_id,),
        )
        assert first.execution_result.status == ExecutionStatus.WAITING_APPROVAL
        assert first_execution["ended_at"] is not None
        assert first_request["state"] == "completed"
        assert task["state"] == "waiting_approval"
        assert attempt["state"] == "waiting"
        request = ApprovalRequest.model_validate_json(approval_row["request_json"])
        interaction_id = str(interaction["interaction_id"])
        interaction_version = int(interaction["version"])

    with ProductionRuntime(
        _config(database), hermes_client_factory=lambda _: client.client
    ) as app:
        approved = _approved_resolution(request)
        with pytest.raises(PermissionError, match="approval_effect_mismatch"):
            app.resolve_interaction(
                command_id="resolve:approval-lifecycle-mismatch",
                interaction_id=interaction_id,
                expected_version=interaction_version,
                resolution=approved.model_copy(
                    update={"effect_request_hash": "sha256:mismatch"}
                ).model_dump(mode="json"),
            )
        assert app.store.query_one(
            "SELECT status FROM interactions WHERE interaction_id = ?",
            (interaction_id,),
        )["status"] == "pending"
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase4_run_requests WHERE source_interaction_id = ?",
            (interaction_id,),
        )[0] == 0
        resolved = app.resolve_interaction(
            command_id="resolve:approval-lifecycle",
            interaction_id=interaction_id,
            expected_version=interaction_version,
            resolution=approved.model_dump(mode="json"),
        )
        assert resolved.attempt_id == submission.attempt_id
        assert resolved.run_request_state == "pending"
        second = asyncio.run(app.run_worker_once())
        assert second.claim.attempt_id == submission.attempt_id
        assert second.invocation.feedback[0]["interaction_id"] == interaction_id
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] == "succeeded"
        assert len(app.business.snapshot()["complaint_tickets"]) == 1
        status = app.task_status(submission.task_id)
        assert status["approval_requests"][0]["status"] == "resolved"
        assert status["approval_resolutions"][0]["decision"] == "approved"
        assert len(status["business_objects"]) == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM executions WHERE task_id = ?",
            (submission.task_id,),
        )[0] == 2


def test_proactive_hermes_approval_uses_the_same_runtime_lifecycle(tmp_path):
    client = ProactiveApprovalProviderClient()
    with ProductionRuntime(
        _config(tmp_path / "proactive-approval.sqlite3"),
        hermes_client_factory=lambda _: client.client,
    ) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:proactive-approval",
            task_id="task:proactive-approval",
            contract=_effect_contract(approval_required=True),
            completion_contract=_effect_completion_contract(),
        )
        first = asyncio.run(app.run_worker_once())
        interaction = app.store.query_one(
            "SELECT * FROM interactions WHERE task_id = ? AND status = 'pending'",
            (submission.task_id,),
        )
        approval_row = app.store.query_one(
            "SELECT request_json FROM phase3_approval_requests WHERE interaction_id = ?",
            (interaction["interaction_id"],),
        )
        assert first.execution_result.status == ExecutionStatus.WAITING_APPROVAL
        request = ApprovalRequest.model_validate_json(approval_row["request_json"])
        app.resolve_interaction(
            command_id="resolve:proactive-approval",
            interaction_id=str(interaction["interaction_id"]),
            expected_version=int(interaction["version"]),
            resolution=_approved_resolution(request).model_dump(mode="json"),
        )
        second = asyncio.run(app.run_worker_once())
        assert second.claim.attempt_id == submission.attempt_id
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] == "succeeded"
        assert len(app.business.snapshot()["complaint_tickets"]) == 1


def test_cancelled_active_worker_cannot_dispatch_or_overwrite_terminal_task(
    tmp_path,
):
    async def scenario() -> None:
        client = EffectProviderClient(block_first_call=True)
        with ProductionRuntime(
            _config(tmp_path / "cancel-active.sqlite3"),
            hermes_client_factory=lambda _: client.client,
        ) as app:
            app.business.seed_normal_complaint()
            submission = app.submit_task(
                command_id="submit:cancel-active",
                task_id="task:cancel-active",
                contract=_effect_contract(),
                completion_contract=_effect_completion_contract(),
            )
            worker = asyncio.create_task(app.run_worker_once())
            assert await asyncio.to_thread(client.started.wait, 5)
            task_before_cancel = app.store.query_one(
                "SELECT version FROM phase3_tasks WHERE task_id = ?",
                (submission.task_id,),
            )
            cancelled = await app.cancel_task(
                command_id="cancel:active",
                task_id=submission.task_id,
                expected_task_version=int(task_before_cancel["version"]),
                reason="operator_cancelled_during_provider_call",
            )
            client.release.set()
            run = await worker

            assert cancelled.task_state == "cancelled"
            assert cancelled.attempt_state == "cancelled"
            assert cancelled.active_execution_ids == (
                run.claim.execution_id,
            )
            task = app.store.query_one(
                "SELECT state, version FROM phase3_tasks WHERE task_id = ?",
                (submission.task_id,),
            )
            attempt = app.store.query_one(
                "SELECT state FROM phase3_attempts WHERE attempt_id = ?",
                (submission.attempt_id,),
            )
            request = app.store.query_one(
                "SELECT state FROM phase4_run_requests WHERE run_request_id = ?",
                (submission.run_request_id,),
            )
            execution = app.store.get_execution(run.claim.execution_id)
            assert task["state"] == "cancelled"
            assert attempt["state"] == "cancelled"
            assert request["state"] == "cancelled"
            assert execution["ended_at"] is not None
            assert app.store.query_one(
                """
                SELECT COUNT(*) FROM execution_events
                WHERE execution_id = ?
                  AND event_type = 'LateExecutionResultRecorded'
                """,
                (run.claim.execution_id,),
            )[0] == 1
            assert app.business.snapshot()["complaint_tickets"] == []
            assert app.store.query_one(
                "SELECT COUNT(*) FROM phase3_external_operations"
            )[0] == 0
            assert app.store.query_one(
                "SELECT COUNT(*) FROM phase4_cancel_intents WHERE command_id = ?",
                (cancelled.command_id,),
            )[0] == 1
            status = app.task_status(submission.task_id)
            assert status["task"]["state"] == "cancelled"
            assert status["external_operations"] == []
            assert status["business_objects"] == []

            replay = await app.cancel_task(
                command_id="cancel:active",
                task_id=submission.task_id,
                expected_task_version=int(task_before_cancel["version"]),
                reason="operator_cancelled_during_provider_call",
            )
            assert replay == cancelled
            with pytest.raises(CommandIdentityConflict, match="identity_conflict"):
                await app.cancel_task(
                    command_id="cancel:active",
                    task_id=submission.task_id,
                    expected_task_version=int(task_before_cancel["version"]),
                    reason="different_reason",
                )

    asyncio.run(scenario())


def test_production_effect_occurrence_limit_denies_second_identity(tmp_path):
    client = EffectProviderClient(double_effect=True)
    with ProductionRuntime(
        _config(tmp_path / "effect-occurrence-limit.sqlite3"),
        hermes_client_factory=lambda _: client.client,
    ) as app:
        app.business.seed_normal_complaint()
        app.submit_task(
            command_id="submit:effect-occurrence-limit",
            task_id="task:effect-occurrence-limit",
            contract=_effect_contract(),
            completion_contract=_effect_completion_contract(),
        )

        asyncio.run(app.run_worker_once())

        second = [
            result
            for result in client.tool_results
            if result.get("tool_name") == "create_complaint_ticket"
        ][-1]
        assert second["error"]["type"] == "effect_occurrence_limit_exceeded"
        assert len(app.business.snapshot()["complaint_tickets"]) == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_external_operations"
        )[0] == 1
        assert app.store.query_one(
            "SELECT COUNT(*) FROM phase3_canonical_effect_requests"
        )[0] == 1


def test_cancel_command_atomically_cancels_pending_interaction(tmp_path):
    async def scenario() -> None:
        provider = WaitingThenTerminalProvider()
        with ProductionRuntime(
            _config(tmp_path / "cancel-waiting.sqlite3"),
            hermes_client_factory=provider.client_for,
        ) as app:
            submission = app.submit_task(
                command_id="submit:cancel-waiting",
                task_id="task:cancel-waiting",
                contract=_contract(),
                completion_contract=_completion_contract(),
            )
            await app.run_worker_once()
            task = app.store.query_one(
                "SELECT version FROM phase3_tasks WHERE task_id = ?",
                (submission.task_id,),
            )
            cancelled = await app.cancel_task(
                command_id="cancel:waiting",
                task_id=submission.task_id,
                expected_task_version=int(task["version"]),
            )
            interaction = app.store.query_one(
                "SELECT status FROM interactions WHERE task_id = ?",
                (submission.task_id,),
            )
            assert cancelled.task_state == "cancelled"
            assert cancelled.attempt_state == "cancelled"
            assert interaction["status"] == "cancelled"
            assert len(cancelled.cancelled_interaction_ids) == 1

    asyncio.run(scenario())


def test_cancel_cli_uses_the_production_cancel_command(tmp_path, capsys):
    database = tmp_path / "cancel-cli.sqlite3"
    contract_path = tmp_path / "cancel-task-contract.json"
    completion_path = tmp_path / "cancel-completion-contract.json"
    contract_path.write_text(_contract().model_dump_json())
    completion_path.write_text(_completion_contract().model_dump_json())

    assert main(
        [
            "--database",
            str(database),
            "submit",
            "--command-id",
            "submit:cancel-cli",
            "--task-id",
            "task:cancel-cli",
            "--contract",
            str(contract_path),
            "--completion-contract",
            str(completion_path),
        ]
    ) == 0
    capsys.readouterr()

    assert main(
        [
            "--database",
            str(database),
            "cancel",
            "--command-id",
            "cancel:cli",
            "--task-id",
            "task:cancel-cli",
            "--expected-task-version",
            "1",
            "--reason",
            "operator_cancelled_from_cli",
        ]
    ) == 0
    output = capsys.readouterr().out
    assert '"task_state":"cancelled"' in output

    with ProductionRuntime(_config(database)) as app:
        task = app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = 'task:cancel-cli'"
        )
        intent = app.store.query_one(
            "SELECT reason FROM phase4_cancel_intents WHERE command_id = 'cancel:cli'"
        )
        assert task["state"] == "cancelled"
        assert intent["reason"] == "operator_cancelled_from_cli"
