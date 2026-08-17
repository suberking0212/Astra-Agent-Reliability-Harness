from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astra.correlation_store import CorrelationConflict, HermesCorrelationStore
from astra.hermes_adapter import plugin

ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "hermes-agent-main" / ".venv" / "bin" / "hermes"
CAPABILITY = "astra_get_latest_customer_order"


@pytest.fixture(autouse=True)
def _reset_plugin_session_state():
    plugin._clear_session()
    yield
    plugin._clear_session()


class _Provider(BaseHTTPRequestHandler):
    requested = False
    def log_message(self, *_): return
    def do_GET(self):
        body = json.dumps({"object": "list", "data": [{"id": "continuity-model"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if not type(self).requested:
            type(self).requested = True
            delta, finish = {"role": "assistant", "tool_calls": [{"index": 0, "id": "capability-1", "type": "function", "function": {"name": CAPABILITY, "arguments": json.dumps({"customer_id": "customer-1"})}}]}, "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": "continuity verified"}, "stop"
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for choice in ({"index": 0, "delta": delta, "finish_reason": None}, {"index": 0, "delta": {}, "finish_reason": finish}):
            payload = json.dumps({"id": "continuity", "object": "chat.completion.chunk", "created": 0, "model": "continuity-model", "choices": [choice]})
            self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class _Runtime(BaseHTTPRequestHandler):
    calls: list[dict] = []
    mapping: dict[str, str] = {}
    def log_message(self, *_): return
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).calls.append(request)
        operation, args = request["operation"], request["args"]
        if operation == "capability":
            assert request["tool_name"] == CAPABILITY
            type(self).mapping[args["_hermes_session_id"]] = "semantic-task-1"
            result = {"ok": True, "customer_id": "customer-1", "order_id": "order-1", "status": "delivered", "receipt_ref": "receipt-1", "evidence_ref": {"type": "execution_receipt", "id": "receipt-1"}}
        elif operation == "correlation_lookup":
            sid = args["hermes_session_id"]; task_id = type(self).mapping.get(sid)
            result = {"ok": True, "correlation": ({"hermes_session_id": sid, "astra_task_id": task_id} if task_id else None)}
        else:
            raise AssertionError(operation)
        body = json.dumps({"result": json.dumps(result)}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


@pytest.mark.skipif(not HERMES.exists(), reason="Hermes test executable is unavailable")
def test_real_hermes_cli_resume_and_continue_preserve_semantic_session_binding(tmp_path: Path):
    _Provider.requested = False; _Runtime.calls = []; _Runtime.mapping = {}
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _Provider); runtime = ThreadingHTTPServer(("127.0.0.1", 0), _Runtime)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (provider, runtime)]
    for thread in threads: thread.start()
    home = tmp_path / "hermes-home"; home.mkdir()
    (home / "config.yaml").write_text(f"model:\n  default: continuity-model\n  provider: custom\n  base_url: http://127.0.0.1:{provider.server_port}/v1\n  api_key: test\n  api_mode: chat_completions\nplugins:\n  enabled:\n    - astra-runtime\n", encoding="utf-8")
    env = {**os.environ, "HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "true", "ASTRA_RUNTIME_ENDPOINT": f"http://127.0.0.1:{runtime.server_port}", "ASTRA_RUNTIME_PLUGIN_AUTH": "auth", "PYTHONPATH": str(ROOT)}
    def run(*args: str):
        return subprocess.run([str(HERMES.parent / "python"), "-m", "astra.hermes_adapter.launch", str(HERMES), "chat", "-Q", "--cli", *args], cwd=ROOT, env=env, capture_output=True, text=True, timeout=45, check=False)
    try:
        created = run("-q", "get the latest order")
        assert created.returncode == 0, created.stdout + created.stderr
        match = re.search(r"(?:Session ID|session_id)[: ]+([A-Za-z0-9_-]+)", created.stdout + created.stderr)
        assert match, created.stdout + created.stderr
        session_id = match.group(1)
        assert run("--resume", session_id, "-q", "resume continuity").returncode == 0
        assert run("--continue", "-q", "continue continuity").returncode == 0
        assert any(c["operation"] == "correlation_lookup" and c["args"].get("hermes_session_id") == session_id for c in _Runtime.calls)
    finally:
        provider.shutdown(); runtime.shutdown()
        for thread in threads: thread.join()


def test_correlation_store_is_idempotent_and_conflicts_fail_closed(tmp_path: Path):
    store = HermesCorrelationStore(tmp_path / "correlation.sqlite3")
    try:
        assert store.bind("hermes-1", "astra-task-1", "test")["astra_task_id"] == "astra-task-1"
        assert store.bind("hermes-1", "astra-task-1", "repeat")["astra_task_id"] == "astra-task-1"
        with pytest.raises(CorrelationConflict): store.bind("hermes-1", "astra-task-2")
        store.mark_status("hermes-1", "terminal")
        assert store.bind("hermes-1", "astra-task-2")["astra_task_id"] == "astra-task-2"
    finally: store.close()


def _context():
    class Context:
        def __init__(self): self.tools, self.hooks = {}, {}
        def register_tool(self, *, name, handler, **_): self.tools[name] = handler
        def register_hook(self, name, callback): self.hooks[name] = callback
    return Context()


@pytest.mark.parametrize(("platform", "events", "expected"), [
    ("cli", (("on_session_start", "session-1"),), "session-1"),
    ("gateway", (("on_session_start", "session-1"),), ""),
    ("cli", (("on_session_start", "session-1"), ("pre_llm_call", "session-2")), ""),
])
def test_semantic_capability_binds_only_unambiguous_cli_session(monkeypatch, platform, events, expected):
    calls = []
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    monkeypatch.setattr(plugin, "_call", lambda operation, args, tool_name=None: calls.append((operation, dict(args), tool_name)) or json.dumps({"ok": True}))
    context = _context(); plugin.register(context)
    for hook, session_id in events: context.hooks[hook](session_id=session_id, platform=platform)
    assert set(context.tools) == {CAPABILITY}
    context.tools[CAPABILITY]({"customer_id": "customer-1"})
    call = next(call for call in calls if call[0] == "capability")
    assert call[1]["_hermes_session_id"] == expected and call[2] == CAPABILITY
