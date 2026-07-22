"""Opt-in Phase 2 smoke test against a real model provider.

This test is intentionally skipped unless ASTRA_RUN_LIVE_PROVIDER=1 and the
explicit provider credentials/configuration are present. It never blocks
ordinary CI and still uses Mock Business Services for business side effects.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
REQUIRED_ENV = (
    "ASTRA_LIVE_BASE_URL",
    "ASTRA_LIVE_API_KEY",
    "ASTRA_LIVE_MODEL",
)

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRA_RUN_LIVE_PROVIDER") != "1"
    or any(not os.environ.get(name) for name in REQUIRED_ENV),
    reason="opt-in live Provider credentials/configuration not supplied",
)


def _live_contracts() -> tuple[TaskContract, CompletionContract]:
    contract = TaskContract.materialize(
        {
            "schema_version": "1",
            "contract_id": "contract-live-provider-complaint",
            "contract_version": "1",
            "task_type": "live_provider_complaint",
            "execution_type": "tool_execution",
            "objective": {
                "description": (
                    "Customer cust-001 reports that order-001 arrived damaged. "
                    "Create the governed complaint ticket and submit the result."
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
                "schema_id": "live.provider.complaint",
                "schema_version": "1",
                "values": {"order_id": "order-001"},
                "content_hash": "sha256:live-provider-input",
            },
            "allowed_capabilities": [
                {
                    "capability_id": "complaints.integration",
                    "capability_version": "1",
                }
            ],
            "resolved_tools": [
                {
                    "tool_name": name,
                    "tool_version": "1",
                    "schema_hash": production_tool_schema_hash(name),
                    "capability_ref": "complaints.integration@1",
                    "access_mode": "effect" if name == "create_complaint_ticket" else "read",
                }
                for name in (
                    "get_order",
                    "search_policy",
                    "create_complaint_ticket",
                )
            ],
            "constraints": [],
            "authorized_effects": [
                {
                    "effect_intent_id": "create-live-provider-complaint",
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
                "contract_id": "completion-live-provider-complaint",
                "contract_version": "1",
            },
            "limits": {
                "max_attempts": 1,
                "task_deadline": "2099-01-01T00:00:00Z",
                "max_executions_per_attempt": 2,
                "max_feedback_cycles": 0,
                "max_reconcile_cycles": 0,
                "max_agent_steps": 12,
            },
        }
    )
    completion = CompletionContract(
        contract_id="completion-live-provider-complaint",
        contract_version="1",
        task_type="live_provider_complaint",
        requirements=(
            CompletionRequirement(
                requirement_id="confirmed-live-provider-effect",
                description="The complaint effect is authoritatively confirmed.",
                evaluator=EvaluatorRef(
                    evaluator_id="astra.authorized_effect_confirmed",
                    evaluator_version="1",
                ),
                configuration={
                    "effect_intent_ref": "create-live-provider-complaint"
                },
                required_evidence=("external_operation", "business_state"),
            ),
        ),
    )
    return contract, completion


def test_live_provider_normal_complaint_smoke(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - astra_bridge\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_ENABLE_PROJECT_PLUGINS", "1")
    database = tmp_path / "live-provider.sqlite3"
    contract, completion = _live_contracts()
    config = ProductionConfig(
        database_path=database,
        hermes_root=HERMES_ROOT,
        provider_config={
            "base_url": os.environ["ASTRA_LIVE_BASE_URL"],
            "api_key": os.environ["ASTRA_LIVE_API_KEY"],
            "provider": os.environ.get("ASTRA_LIVE_PROVIDER", "custom"),
            "api_mode": os.environ.get(
                "ASTRA_LIVE_API_MODE", "chat_completions"
            ),
            "model": os.environ["ASTRA_LIVE_MODEL"],
        },
        hermes_session_database_path=database.with_suffix(".hermes.sqlite3"),
    )
    with ProductionRuntime(config) as app:
        app.business.seed_normal_complaint()
        submission = app.submit_task(
            command_id="submit:live-provider",
            task_id="task:live-provider",
            contract=contract,
            completion_contract=completion,
        )
        import asyncio

        result = asyncio.run(app.run_worker_once())
        assert result is not None
        assert app.store.query_one(
            "SELECT state FROM phase3_tasks WHERE task_id = ?",
            (submission.task_id,),
        )["state"] == "succeeded"
        assert len(app.business.snapshot()["complaint_tickets"]) == 1
        assert app.store.query_one(
            "SELECT status FROM phase3_external_operations WHERE task_id = ?",
            (submission.task_id,),
        )["status"] == "confirmed"
