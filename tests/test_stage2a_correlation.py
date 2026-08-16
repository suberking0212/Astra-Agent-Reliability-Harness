from __future__ import annotations

import json
import re
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astra.correlation_store import CorrelationConflict, HermesCorrelationStore
from astra.hermes_adapter import plugin

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "hermes-agent-main" / ".venv" / "bin" / "hermes"


@pytest.fixture(autouse=True)
def _reset_plugin_session_state():
    plugin._clear_session()
    yield
    plugin._clear_session()


class _ContinuityProvider(BaseHTTPRequestHandler):
    requests: list[dict] = []
    def log_message(self, *_): return
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "continuity-model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).requests.append(request)
        has_tool = any(m.get("role") == "tool" for m in request.get("messages", []))
        if not has_tool:
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "submit-1", "type": "function", "function": {"name": "astra_submit_task", "arguments": json.dumps({"command_id": "continuity-command", "task_id": "continuity-task", "contract": {}})}}]}
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "continuity verified"}, "stop"
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for choice in ({"index": 0, "delta": delta, "finish_reason": None}, {"index": 0, "delta": {}, "finish_reason": finish}):
            payload = json.dumps({"id": "continuity", "object": "chat.completion.chunk", "created": 0, "model": "continuity-model", "choices": [choice]})
            self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class _ContinuityRuntime(BaseHTTPRequestHandler):
    calls: list[dict] = []
    mapping: dict[str, str] = {}
    def log_message(self, *_): return
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).calls.append(request)
        op, args = request["operation"], request["args"]
        if op == "submit": result = {"ok": True, "task_id": "continuity-task", "business_status": "running", "next_action": "none"}
        elif op == "correlation_bind":
            sid, tid = args["hermes_session_id"], args["astra_task_id"]
            if sid in type(self).mapping and type(self).mapping[sid] != tid: result = {"ok": False, "error": {"message": "correlation_conflict"}}
            else: type(self).mapping[sid] = tid; result = {"ok": True, "correlation": {"hermes_session_id": sid, "astra_task_id": tid}}
        elif op == "correlation_lookup": result = {"ok": True, "correlation": ({"hermes_session_id": args["hermes_session_id"], "astra_task_id": type(self).mapping[args["hermes_session_id"]]} if args["hermes_session_id"] in type(self).mapping else None)}
        else: result = {"ok": True}
        body = json.dumps({"result": json.dumps(result)}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


@pytest.mark.skipif(not HERMES.exists(), reason="Hermes test executable is unavailable")
def test_real_hermes_cli_resume_and_continue_preserve_astra_task(tmp_path: Path):
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ContinuityProvider)
    runtime = ThreadingHTTPServer(("127.0.0.1", 0), _ContinuityRuntime)
    threading.Thread(target=provider.serve_forever, daemon=True).start(); threading.Thread(target=runtime.serve_forever, daemon=True).start()
    home = tmp_path / "hermes-home"; home.mkdir()
    (home / "config.yaml").write_text(f"""model:\n  default: continuity-model\n  provider: custom\n  base_url: http://127.0.0.1:{provider.server_port}/v1\n  api_key: test\n  api_mode: chat_completions\nplugins:\n  enabled:\n    - astra-runtime\n""")
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "true", "ASTRA_RUNTIME_ENDPOINT": f"http://127.0.0.1:{runtime.server_port}", "ASTRA_RUNTIME_PLUGIN_AUTH": "auth", "PYTHONPATH": str(ROOT)}
    def run(*args): return subprocess.run([str(HERMES), "chat", "-Q", "--cli", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=45, check=False)
    try:
        created = run("-q", "submit continuity task")
        assert created.returncode == 0, created.stdout + created.stderr
        match = re.search(r"(?:Session ID|session_id)[: ]+([A-Za-z0-9_-]+)", created.stdout + created.stderr)
        assert match, created.stdout + created.stderr
        session_id = match.group(1)
        resumed = run("--resume", session_id, "-q", "resume continuity")
        continued = run("--continue", "-q", "continue continuity")
        assert resumed.returncode == continued.returncode == 0, resumed.stdout + resumed.stderr + continued.stdout + continued.stderr
        lookups = [c for c in _ContinuityRuntime.calls if c["operation"] == "correlation_lookup" and c["args"].get("hermes_session_id") == session_id]
        assert lookups and _ContinuityRuntime.mapping[session_id] == "continuity-task"
    finally:
        provider.shutdown(); runtime.shutdown()


def test_correlation_store_is_idempotent_and_conflicts_fail_closed(tmp_path: Path):
    store = HermesCorrelationStore(tmp_path / "correlation.sqlite3")
    try:
        first = store.bind("hermes-1", "astra-task-1", "test")
        again = store.bind("hermes-1", "astra-task-1", "repeat")
        assert first["astra_task_id"] == again["astra_task_id"] == "astra-task-1"
        assert store.lookup("unknown") is None
        with pytest.raises(CorrelationConflict, match="already bound"):
            store.bind("hermes-1", "astra-task-2")
        assert store.lookup("hermes-1")["astra_task_id"] == "astra-task-1"
    finally:
        store.close()


def test_terminal_correlation_can_be_rebound_but_active_one_cannot(tmp_path: Path):
    store = HermesCorrelationStore(tmp_path / "correlation.sqlite3")
    try:
        store.bind("hermes-1", "astra-task-1")
        with pytest.raises(CorrelationConflict):
            store.bind("hermes-1", "astra-task-2")
        store.mark_status("hermes-1", "terminal")
        rebound = store.bind("hermes-1", "astra-task-2")
        assert rebound["astra_task_id"] == "astra-task-2"
        assert rebound["status"] == "active"
    finally:
        store.close()


def test_plugin_reuses_the_active_session_task_before_submit(monkeypatch):
    calls: list[tuple[str, dict]] = []
    plugin._clear_session()
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    def call(operation, args, tool_name=None):
        calls.append((operation, dict(args)))
        if operation == "correlation_lookup":
            return json.dumps({"ok": True, "correlation": {"astra_task_id": "existing-task"}})
        if operation == "status":
            return json.dumps({"ok": True, "task_id": "existing-task", "business_status": "running"})
        raise AssertionError(operation)
    monkeypatch.setattr(plugin, "_call", call)
    class Context:
        def __init__(self): self.tools = {}; self.hooks = {}
        def register_tool(self, *, name, handler, **kwargs): self.tools[name] = handler
        def register_hook(self, name, callback): self.hooks[name] = callback
    ctx = Context(); plugin.register(ctx)
    ctx.hooks["on_session_start"](session_id="hermes-reuse")
    result = json.loads(ctx.tools["astra_submit_task"]({"objective": "repeat"}))
    assert result["task_id"] == "existing-task"
    assert result["reused"] is True
    assert not any(operation == "submit" for operation, _ in calls)


def test_plugin_binds_only_after_successful_submit(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    monkeypatch.setattr(plugin, "_call", lambda operation, args, tool_name=None: calls.append((operation, dict(args))) or
                        (json.dumps({"ok": True, "task_id": "astra-7"}) if operation == "submit" else json.dumps({"ok": True})))
    class Context:
        def __init__(self): self.tools = {}; self.hooks = {}
        def register_tool(self, *, name, handler, **kwargs): self.tools[name] = handler
        def register_hook(self, name, callback): self.hooks[name] = callback
    ctx = Context()
    plugin.register(ctx)
    ctx.hooks["on_session_start"](session_id="hermes-7")
    result = json.loads(ctx.tools["astra_submit_task"]({"command_id": "c", "task_id": "astra-7", "contract": {}}))
    assert result["task_id"] == "astra-7"
    assert any(op == "correlation_bind" and args["hermes_session_id"] == "hermes-7" for op, args in calls)


def test_gateway_hook_does_not_set_a_bindable_session(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    monkeypatch.setattr(plugin, "_call", lambda operation, args, tool_name=None: calls.append((operation, dict(args))) or
                        (json.dumps({"ok": True, "task_id": "astra-gw"}) if operation == "submit" else json.dumps({"ok": True})))
    class Context:
        def __init__(self): self.tools = {}; self.hooks = {}
        def register_tool(self, *, name, handler, **kwargs): self.tools[name] = handler
        def register_hook(self, name, callback): self.hooks[name] = callback
    ctx = Context(); plugin.register(ctx)
    ctx.hooks["on_session_start"](session_id="gateway-session", platform="gateway")
    ctx.tools["astra_submit_task"]({"command_id": "c", "task_id": "astra-gw", "contract": {}})
    assert not any(op == "correlation_bind" for op, _ in calls)


def test_session_switch_is_ambiguous_until_reset(monkeypatch):
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    monkeypatch.setattr(plugin, "_call", lambda operation, args, tool_name=None: calls.append((operation, dict(args))) or
                        (json.dumps({"ok": True, "task_id": "astra-x"}) if operation == "submit" else json.dumps({"ok": True})))
    class Context:
        def __init__(self): self.tools = {}; self.hooks = {}
        def register_tool(self, *, name, handler, **kwargs): self.tools[name] = handler
        def register_hook(self, name, callback): self.hooks[name] = callback
    ctx = Context(); plugin.register(ctx)
    ctx.hooks["on_session_start"](session_id="cli-a", platform="cli")
    ctx.hooks["pre_llm_call"](session_id="cli-b", platform="cli")
    ctx.tools["astra_submit_task"]({"command_id": "c", "task_id": "astra-x", "contract": {}})
    assert not any(op == "correlation_bind" for op, _ in calls)
