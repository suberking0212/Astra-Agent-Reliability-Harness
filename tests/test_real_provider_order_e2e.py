"""Production-evidence acceptance: real Hermes/OpenRouter order read."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from astra.production_evidence import ProductionEvidenceReport
from astra.hermes_composition import build_runtime_service_from_environment
from astra.runtime_service import _Handler as RuntimeHandler
from astra.storage import AstraStore
from business_sandbox.server import SandboxServer, SandboxStore

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "hermes-agent-main" / ".venv" / "bin" / "hermes"
HERMES_HOME = Path.home() / ".astra" / "hermes"


@pytest.mark.skipif(
    not HERMES.exists() or not (HERMES_HOME / ".env").exists(),
    reason="real Hermes Provider credentials are not configured",
)
def test_real_provider_hermes_native_order_read(tmp_path: Path):
    sandbox_store = SandboxStore(tmp_path / "business.sqlite3")
    sandbox_store.seed()
    sandbox = SandboxServer(("127.0.0.1", 0), sandbox_store, "e2e-sandbox-token")
    threading.Thread(target=sandbox.serve_forever, daemon=True).start()

    previous = dict(os.environ)
    os.environ.update({
        "ASTRA_DATABASE": str(tmp_path / "astra.sqlite3"),
        "ASTRA_BUSINESS_SANDBOX_ENDPOINT": f"http://127.0.0.1:{sandbox.server_address[1]}",
        "ASTRA_BUSINESS_SANDBOX_TOKEN": "e2e-sandbox-token",
        "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN": "commerce.real-provider",
        "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN": "support.real-provider",
        "ASTRA_RUNTIME_PLUGIN_AUTH": "real-provider-runtime-auth",
    })
    RuntimeHandler.service = build_runtime_service_from_environment()
    runtime_server = __import__("http.server", fromlist=["ThreadingHTTPServer"]).ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    threading.Thread(target=runtime_server.serve_forever, daemon=True).start()

    home = tmp_path / "hermes-home"
    shutil.copytree(HERMES_HOME, home, dirs_exist_ok=True)
    env = {**previous, "HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "true",
           "ASTRA_RUNTIME_ENDPOINT": f"http://127.0.0.1:{runtime_server.server_address[1]}",
           "ASTRA_RUNTIME_PLUGIN_AUTH": "real-provider-runtime-auth", "PYTHONPATH": str(ROOT)}
    for key in ("ASTRA_BUSINESS_SANDBOX_ENDPOINT", "ASTRA_BUSINESS_SANDBOX_TOKEN",
                "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN", "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN"):
        env.pop(key, None)
    try:
        completed = subprocess.run(
            [str(HERMES), "chat", "-q", "查 cust-s12-s13-001 最近一笔订单状态"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=120, check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "order-s12-s13-001" in completed.stdout
        assert "delivered" in completed.stdout

        store = AstraStore(tmp_path / "astra.sqlite3")
        try:
            rows = store.query_all("SELECT task_id, contract_json, state FROM phase3_tasks")
            task = next(row for row in rows if "cust-s12-s13-001" in row["contract_json"])
            contract = json.loads(task["contract_json"])
            assert [tool["tool_name"] for tool in contract["resolved_tools"]] == ["list_customer_orders"]
            report = ProductionEvidenceReport(
                provider_mode="openrouter_real",
                runtime_path="hermes_native_cli->plugin->runtime->governance->gateway->sandbox",
                business_boundary="commerce.orders.read",
                fault_mode="none",
                authoritative_facts=({"source": "phase3_tasks", "task_id": task["task_id"], "state": task["state"]},),
                eligible_claims=("real_provider_tool_loop", "governed_order_read", "sandbox_backed_result"),
                prohibited_claims=("runtime_recovery", "network_timeout_safety", "multi_domain_routing"),
            )
            assert report.provider_mode == "openrouter_real"
        finally:
            store.close()
    finally:
        runtime_server.shutdown(); sandbox.shutdown(); RuntimeHandler.service.close()
        os.environ.clear(); os.environ.update(previous)
