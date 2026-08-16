from __future__ import annotations

import json
from types import SimpleNamespace

from astra.artifact_observations import from_post_tool_call
from astra.correlation_store import HermesCorrelationStore
from astra.runtime_service import RuntimeService
from astra.storage import AstraStore
from astra.hermes_adapter import plugin


def _hook(tool_name: str, args: dict, result: dict, tool_call_id: str = "call-1"):
    return {
        "session_id": "session-1", "task_id": "hermes-task-1", "tool_call_id": tool_call_id,
        "turn_id": "turn-1", "api_request_id": "api-1", "tool_name": tool_name,
        "args": args, "result": json.dumps(result), "status": "ok", "duration_ms": 1,
    }


def _service(tmp_path):
    store = AstraStore(tmp_path / "astra.sqlite3")
    correlations = HermesCorrelationStore(tmp_path / "correlation.sqlite3")
    service = RuntimeService.__new__(RuntimeService)
    service.app = SimpleNamespace(store=store)
    service.correlations = correlations
    return service, store, correlations


def test_skill_view_observation_is_content_free_and_bound(tmp_path):
    service, store, correlations = _service(tmp_path)
    correlations.bind("session-1", "task-1", "test", "attempt-1", "execution-1")
    observation = from_post_tool_call(_hook("skill_view", {"name": "order-help"}, {
        "success": True, "name": "order-help", "path": "commerce/order-help/SKILL.md",
        "skill_dir": "/private/skill", "content": "must never persist",
    }))
    assert observation is not None
    assert service.artifact_action_observation(observation.__dict__)["correlation_status"] == "bound"
    row = store.query_one("SELECT * FROM artifact_action_observations WHERE tool_call_id = ?", ("call-1",))
    assert row["task_id"] == "task-1" and row["attempt_id"] == "attempt-1" and row["execution_id"] == "execution-1"
    assert row["artifact_ref"] == "commerce/order-help/SKILL.md" and row["action"] == "view"
    assert "must never persist" not in row["metadata_json"]
    store.close(); correlations.close()


def test_replayed_tool_call_is_idempotent_and_unbound_never_guesses(tmp_path):
    service, store, correlations = _service(tmp_path)
    observation = from_post_tool_call(_hook("skills_list", {"category": "commerce"}, {"success": True}))
    assert observation is not None
    assert service.artifact_action_observation(observation.__dict__)["correlation_status"] == "unbound"
    assert service.artifact_action_observation(observation.__dict__)["correlation_status"] == "unbound"
    rows = store.query_all("SELECT * FROM artifact_action_observations")
    assert len(rows) == 1 and rows[0]["task_id"] is None and rows[0]["artifact_ref"] is None
    store.close(); correlations.close()


def test_memory_actions_are_explicit_and_not_snapshot_reuse():
    assert from_post_tool_call(_hook("memory", {"action": "search", "target": "memory"}, {})).action == "search"
    assert from_post_tool_call(_hook("memory", {"action": "read", "target": "memory"}, {})).action == "read"
    assert from_post_tool_call(_hook("memory", {"action": "add", "target": "memory"}, {})).action == "write"
    assert from_post_tool_call(_hook("memory", {"action": "remove", "target": "memory"}, {})).action == "delete"


def test_skill_manage_only_records_confirmed_actions():
    assert from_post_tool_call(_hook("skill_manage", {"action": "create", "name": "x"}, {"success": False})) is None
    observation = from_post_tool_call(_hook("skill_manage", {"action": "create", "name": "x"}, {"success": True}))
    assert observation is not None and observation.action == "create" and observation.artifact_ref == "x"


def test_semantic_capability_binds_parent_session_to_started_execution(tmp_path):
    service, store, correlations = _service(tmp_path)
    service._context = lambda task_id: SimpleNamespace(attempt_id="attempt-1", execution_id="execution-1")
    service.capability_broker = SimpleNamespace(
        invoke=lambda _name, _args, on_started: (on_started("task-1"), {"ok": True})[1]
    )
    assert service.capability("astra_get_latest_customer_order", {"_hermes_session_id": "session-1"}) == {"ok": True}
    row = correlations.lookup("session-1")
    assert row is not None
    assert row["astra_task_id"] == "task-1" and row["attempt_id"] == "attempt-1" and row["execution_id"] == "execution-1"
    store.close(); correlations.close()


def test_plugin_observation_hook_is_fail_open(monkeypatch):
    class Context:
        def __init__(self): self.hooks = {}; self.tools = {}
        def register_hook(self, name, callback): self.hooks[name] = callback
        def register_tool(self, **kwargs): self.tools[kwargs["name"]] = kwargs

    monkeypatch.setattr(plugin, "_transport_credential_cache", "transport")
    monkeypatch.setattr(plugin, "_call", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    context = Context()
    plugin.register(context)
    assert context.hooks["post_tool_call"](**_hook("skill_view", {"name": "x"}, {"success": True, "name": "x"})) is None
