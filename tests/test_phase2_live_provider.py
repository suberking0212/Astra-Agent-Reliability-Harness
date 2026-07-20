"""Opt-in Phase 2 smoke test against a real model provider.

This test is intentionally skipped unless ASTRA_RUN_LIVE_PROVIDER=1 and the
explicit provider credentials/configuration are present. It never blocks
ordinary CI and still uses Mock Business Services for business side effects.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

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


def test_live_provider_normal_complaint_smoke(tmp_path, monkeypatch):
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

    store = AstraStore(tmp_path / "live-provider.sqlite3")
    business = MockBusinessService(store)
    business.seed_normal_complaint()
    gateway = AstraToolGateway(store, business)
    executor = HermesExecutor(
        hermes_root=HERMES_ROOT,
        store=store,
        gateway=gateway,
        validator=MinimalResultValidator(store, business),
        budget=BudgetLedger(store),
        trace=NeutralTraceCollector(store),
    )
    invocation = RuntimeInvocation(
        execution_id="exec-live-provider",
        task_id="task-live-provider",
        attempt_id="attempt-live-provider",
        user_request=(
            "Customer cust-001 reports that order-001 arrived damaged after the "
            "normal return window. Investigate the applicable policy, create a "
            "complaint ticket if supported, and submit a result with receipt evidence."
        ),
        task_contract={
            "task_type": "complaint_resolution",
            "execution_type": "tool_execution",
            "completion_requirements": [
                "complaint ticket exists for cust-001 and order-001",
                "the result cites execution receipts",
            ],
        },
        allowed_tools=(
            "get_customer",
            "get_order",
            "search_policy",
            "create_complaint_ticket",
            "get_complaint_ticket",
        ),
        provider_config={
            "base_url": os.environ["ASTRA_LIVE_BASE_URL"],
            "api_key": os.environ["ASTRA_LIVE_API_KEY"],
            "provider": os.environ.get("ASTRA_LIVE_PROVIDER", "custom"),
            "api_mode": os.environ.get(
                "ASTRA_LIVE_API_MODE", "chat_completions"
            ),
            "model": os.environ["ASTRA_LIVE_MODEL"],
        },
        limits={
            "max_agent_steps": 12,
            "budget_mode": "conservative_limit",
            "provider_request_limit": 12,
        },
    )
    events = []

    async def sink(event):
        events.append(event)

    result = asyncio.run(executor.execute(invocation, sink))
    assert result.task_outcome_validated is True
    assert result.result_receipt and result.result_receipt["valid"] is True
    assert len(business.snapshot()["complaint_tickets"]) == 1
    assert any(event.event_type == "ResultValidation" for event in events)
