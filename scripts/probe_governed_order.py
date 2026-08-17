"""Drive the REAL Astra governed pipeline (RuntimeService -> ToolGateway ->
BusinessSandbox) against a temp seeded SQLite, exercising the exact governed
task lifecycle. No live runtime auth needed. Same seed constants as the
harness default sandbox (commerce.local-sandbox domain).
"""
from __future__ import annotations
import json
import os
import tempfile
import uuid
from pathlib import Path

if os.environ.get("ASTRA_ALLOW_TEST_PROBE") != "1":
    raise SystemExit("probe_governed_order.py is test-only; use the Hermes business protocol")

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from astra.runtime_service import RuntimeService
from business_sandbox.server import SandboxStore, SandboxServer

CUSTOMER = "cust-s12-s13-001"
COMMERCE_DOMAIN = "commerce.local-sandbox"  # harness default (start_astra_cli.sh:42)

# 1) Seed an independent sandbox SQLite (same seed constants as the live one).
tmp = Path(tempfile.mkdtemp(prefix="astra-order-probe-"))
sandbox_db = tmp / "business-sandbox.sqlite3"
store = SandboxStore(sandbox_db)
store.seed()

# 2) Bring up the sandbox HTTP server (the runtime is the unsandboxed parent).
sandbox = SandboxServer(("127.0.0.1", 0), store, "local-sandbox-token")
import threading
threading.Thread(target=sandbox.serve_forever, daemon=True).start()
sandbox_endpoint = f"http://127.0.0.1:{sandbox.server_address[1]}"

# 3) Stand up the REAL RuntimeService (owns AstraStore + ToolGateway + sandbox adapter).
astra_db = tmp / "astra.sqlite3"
os.environ.update({
    "ASTRA_DATABASE": str(astra_db),
    "ASTRA_BUSINESS_SANDBOX_ENDPOINT": sandbox_endpoint,
    "ASTRA_BUSINESS_SANDBOX_TOKEN": "local-sandbox-token",
    "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN": COMMERCE_DOMAIN,
    "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN": "support.local-sandbox",
    "ASTRA_RUNTIME_PLUGIN_AUTH": "probe-plugin-auth",
})
svc = RuntimeService()

# 4) Governed task lifecycle (the same steps astra_submit_task / astra_business_* run).
command_id = "hermes-command:" + uuid.uuid4().hex
task_id = "hermes-task:" + uuid.uuid4().hex

submit = svc.submit({
    "objective": "查询客户 cust-s12-s13-001 最近一笔订单的状态",
    "subjects": [{"authority_domain": COMMERCE_DOMAIN, "type": "customer", "id": CUSTOMER}],
    "requested_tools": ["list_customer_orders"],
    "command_id": command_id,
    "task_id": task_id,
})
print("SUBMIT:", json.dumps(submit, ensure_ascii=False))

allowed = submit["next_action"]["allowed_tools"]
assert "astra_business_list_customer_orders" in allowed, allowed

orders = svc.business("list_customer_orders", {
    "task_id": task_id,
    "arguments": {"customer_id": CUSTOMER},
})
result = orders.get("result", {})
orders_list = (result or {}).get("result", {}).get("orders")
print("ORDERS RAW:", json.dumps(orders, ensure_ascii=False))

# Pick most recent order (seed has 1; sort defensively).
if orders_list:
    latest = sorted(orders_list, key=lambda o: (o.get("delivered_at") or ""))[-1]
    print("\n=== LATEST ORDER ===")
    for k in ("order_id", "customer_id", "item_name", "status", "delivered_at", "damage_reported"):
        print(f"  {k}: {latest.get(k)}")
else:
    print("No orders returned")

svc.close()
sandbox.shutdown()
