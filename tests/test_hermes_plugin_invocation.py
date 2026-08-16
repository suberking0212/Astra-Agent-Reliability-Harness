"""Black-box verification for the supported Hermes public-plugin boundary."""

from __future__ import annotations

import json
import os
import pty
import select
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from astra.storage import AstraStore
from astra.hermes_composition import build_runtime_service_from_environment
from astra.runtime_service import _Handler as RuntimeHandler
from business_sandbox.server import SandboxServer, SandboxStore, SandboxHandler


ROOT = Path(__file__).resolve().parents[1]
HERMES = ROOT / "hermes-agent-main" / ".venv" / "bin" / "hermes"
STATES = {
    "waiting_input": "等待输入",
    "waiting_approval": "等待审批",
    "succeeded": "已成功",
    "failed": "已失败",
    "cancelled": "已取消",
}


class _ScriptedProvider(BaseHTTPRequestHandler):
    """OpenAI-compatible fixture that deterministically requests the plugin tool."""

    task_ids: list[str] = []
    requests: list[dict[str, Any]] = []

    def log_message(self, *_: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._send({"object": "list", "data": [{"id": "plugin-test-model"}]})

    def do_POST(self) -> None:  # noqa: N802
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size))
        type(self).requests.append(request)
        completed_calls = sum(
            message.get("role") == "tool" for message in request.get("messages", [])
        )
        if completed_calls < len(type(self).task_ids):
            task_id = type(self).task_ids[completed_calls]
            delta = {
                "role": "assistant",
                "tool_calls": [{
                    "index": 0,
                    "id": f"call-{completed_calls}",
                    "type": "function",
                    "function": {
                        "name": "astra_runtime_status",
                        "arguments": json.dumps({"task_id": task_id}),
                    },
                }],
            }
            finish_reason = "tool_calls"
        else:
            delta = {"role": "assistant", "content": "plugin invocation verified"}
            finish_reason = "stop"
        self._send_stream(delta, finish_reason)

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self, delta: dict[str, Any], finish_reason: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for choice in (
            {"index": 0, "delta": delta, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": finish_reason},
        ):
            payload = json.dumps({
                "id": "plugin-test",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "plugin-test-model",
                "choices": [choice],
            })
            self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def _seed_task_states(database: Path) -> list[str]:
    store = AstraStore(database)
    try:
        now = datetime.now(timezone.utc).isoformat()
        task_ids = [f"plugin-status-{state}" for state in STATES]
        with store.transaction() as connection:
            for task_id, state in zip(task_ids, STATES, strict=True):
                connection.execute(
                    """
                    INSERT INTO phase3_tasks(
                        task_id, contract_json, contract_id, contract_version,
                        contract_hash, state, version, current_attempt_id,
                        created_at, updated_at
                    ) VALUES (?, '{}', 'plugin-test', '1', 'sha256:plugin-test', ?, 1,
                              ?, ?, ?)
                    """,
                    (task_id, state, f"attempt:{task_id}", now, now),
                )
        return task_ids
    finally:
        store.close()


@pytest.mark.skipif(not HERMES.exists(), reason="Hermes test executable is unavailable")
@pytest.mark.skip(reason="Superseded by the semantic Broker surface; Runtime status is no longer model-visible.")
def test_hermes_project_plugin_discovers_registers_and_invokes_runtime_status(tmp_path: Path):
    """Exercise the Hermes CLI and public project-plugin API without internals."""
    database = tmp_path / "astra.sqlite3"
    task_ids = _seed_task_states(database)
    _ScriptedProvider.task_ids = task_ids
    _ScriptedProvider.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedProvider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        """model:
  default: plugin-test-model
  provider: custom
  base_url: http://127.0.0.1:%d/v1
  api_key: plugin-test-key
  api_mode: chat_completions
plugins:
  enabled:
    - astra-runtime
""" % server.server_port,
        encoding="utf-8",
    )
    privileged_environment = {
        **os.environ,
        "ASTRA_DATABASE": str(database),
        "ASTRA_BUSINESS_SANDBOX_ENDPOINT": "http://127.0.0.1:8765",
        "ASTRA_BUSINESS_SANDBOX_TOKEN": "plugin-test-token",
        "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN": "commerce.plugin-test",
        "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN": "support.plugin-test",
        "ASTRA_RUNTIME_PLUGIN_AUTH": "test-plugin-machine-authentication",
        "PYTHONPATH": str(ROOT),
    }
    previous_environment = dict(os.environ)
    os.environ.clear()
    os.environ.update(privileged_environment)
    try:
        RuntimeHandler.service = build_runtime_service_from_environment()
    finally:
        os.environ.clear()
        os.environ.update(previous_environment)
    runtime_server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler)
    runtime_thread = threading.Thread(target=runtime_server.serve_forever, daemon=True)
    runtime_thread.start()
    environment = {
        **os.environ,
        "HERMES_HOME": str(hermes_home),
        "HERMES_ENABLE_PROJECT_PLUGINS": "true",
        "ASTRA_RUNTIME_ENDPOINT": f"http://127.0.0.1:{runtime_server.server_port}",
        "ASTRA_RUNTIME_PLUGIN_AUTH": "test-plugin-machine-authentication",
        "PYTHONPATH": str(ROOT),
    }
    for secret in ("ASTRA_BUSINESS_SANDBOX_ENDPOINT", "ASTRA_BUSINESS_SANDBOX_TOKEN",
                   "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN", "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN"):
        environment.pop(secret, None)
    unauthenticated = Request(
        f"http://127.0.0.1:{runtime_server.server_port}/v1/plugin",
        data=b'{"operation":"status","args":{"task_id":"plugin-status-succeeded"}}',
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with pytest.raises(HTTPError) as rejected:
        urlopen(unauthenticated)
    assert rejected.value.code == 401
    wrong = Request(
        f"http://127.0.0.1:{runtime_server.server_port}/v1/plugin",
        data=b'{"operation":"status","args":{"task_id":"plugin-status-succeeded"}}',
        headers={"Content-Type": "application/json", "X-Astra-Plugin-Auth": "wrong-auth"}, method="POST",
    )
    with pytest.raises(HTTPError) as rejected_wrong:
        urlopen(wrong)
    assert rejected_wrong.value.code == 401
    try:
        completed = subprocess.run(
            [str(HERMES), "-t", "astra_runtime", "chat", "-q", "Read each requested Astra Runtime status."],
            cwd=ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        server.shutdown()
        thread.join()
        runtime_server.shutdown()
        runtime_thread.join()
        RuntimeHandler.service.app.close()

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "plugin invocation verified" in completed.stdout, (
        completed.stdout + completed.stderr + repr(_ScriptedProvider.requests)
    )
    assert _ScriptedProvider.requests
    assert any(
        tool.get("function", {}).get("name") == "astra_runtime_status"
        for request in _ScriptedProvider.requests
        for tool in request.get("tools", [])
    )
    schema_request = next(request for request in _ScriptedProvider.requests if request.get("tools"))
    final_schema = {
        tool["function"]["name"]: tool["function"]
        for tool in schema_request["tools"]
        if tool.get("type") == "function"
    }
    assert "Discover the Astra Governed Capabilities" in (
        final_schema["astra_runtime_capabilities"]["description"]
    )
    assert "authoritative result from an external governed capability" in (
        final_schema["astra_submit_task"]["description"]
    )
    tool_results_by_call = {
        message["tool_call_id"]: json.loads(message["content"])
        for request in _ScriptedProvider.requests
        for message in request.get("messages", [])
        if message.get("role") == "tool"
    }
    tool_results = [tool_results_by_call[f"call-{index}"] for index in range(len(task_ids))]
    assert tool_results == [
        {"ok": True, "task_id": task_id, "business_status": state,
         "next_action": ("resolve_input" if state == "waiting_input" else
                         "resolve_approval" if state == "waiting_approval" else "none"),
         "pending_interactions": []}
        for state, task_id in zip(STATES, task_ids, strict=True)
    ]


class _BusinessE2EProvider(BaseHTTPRequestHandler):
    requests: list[dict[str, Any]] = []
    request_times: list[float] = []

    def log_message(self, *_: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        self._send_json({"object": "list", "data": [{"id": "business-e2e-model"}]})

    def do_POST(self) -> None:  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).requests.append(request)
        type(self).request_times.append(time.perf_counter())
        tool_messages = [m for m in request.get("messages", []) if m.get("role") == "tool"]
        if tool_messages:
            text = "订单 order-s12-s13-001 状态为 delivered"
            return self._send_stream({"role": "assistant", "content": text}, "stop")
        call = {"name": "astra_get_latest_customer_order", "arguments": {
            "customer_id": "cust-s12-s13-001"}}
        self._send_stream({"role": "assistant", "tool_calls": [{"index": 0, "id": f"e2e-{len(tool_messages)}", "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False)}}]}, "tool_calls")

    def _send_json(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode(); self.send_response(200)
        self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _send_stream(self, delta: dict[str, Any], finish_reason: str) -> None:
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        for choice in ({"index": 0, "delta": delta, "finish_reason": None}, {"index": 0, "delta": {}, "finish_reason": finish_reason}):
            payload = json.dumps({"id": "business-e2e", "object": "chat.completion.chunk", "created": 0, "model": "business-e2e-model", "choices": [choice]})
            self.wfile.write(f"data: {payload}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class _TimedRuntimeHandler(RuntimeHandler):
    timings: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        started = time.perf_counter()
        size = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(size)
        try:
            payload = json.loads(body)
            operation = payload.get("operation")
        except (TypeError, ValueError, json.JSONDecodeError):
            operation = None
        # Rebuild the body because the base handler reads it itself.
        self.rfile = __import__("io").BytesIO(body)
        try:
            super().do_POST()
        finally:
            type(self).timings.append({
                "operation": operation,
                "elapsed_s": time.perf_counter() - started,
            })


@pytest.mark.skipif(not HERMES.exists(), reason="Hermes test executable is unavailable")
def test_real_hermes_cli_uses_semantic_broker_for_governed_sandbox_read(tmp_path: Path):
    _BusinessE2EProvider.requests = []
    _BusinessE2EProvider.request_times = []
    _TimedRuntimeHandler.timings = []
    sandbox_store = SandboxStore(tmp_path / "business.sqlite3"); sandbox_store.seed()
    sandbox = SandboxServer(("127.0.0.1", 0), sandbox_store, "sandbox-e2e-token")
    threading.Thread(target=sandbox.serve_forever, daemon=True).start()
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _BusinessE2EProvider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    database = tmp_path / "astra.sqlite3"
    previous = dict(os.environ)
    os.environ.update({"ASTRA_DATABASE": str(database), "ASTRA_BUSINESS_SANDBOX_ENDPOINT": f"http://127.0.0.1:{sandbox.server_address[1]}", "ASTRA_BUSINESS_SANDBOX_TOKEN": "sandbox-e2e-token", "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN": "commerce.e2e", "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN": "support.e2e", "ASTRA_RUNTIME_PLUGIN_AUTH": "runtime-e2e-auth"})
    RuntimeHandler.service = build_runtime_service_from_environment()
    runtime_server = ThreadingHTTPServer(("127.0.0.1", 0), _TimedRuntimeHandler); threading.Thread(target=runtime_server.serve_forever, daemon=True).start()
    home = tmp_path / "hermes-home"; home.mkdir()
    # A persisted plugin toolset is an explicit opt-out in Hermes unless the
    # active session's allowlist names it too. This reproduces the interactive
    # failure state that the launcher must converge away from.
    (home / "config.yaml").write_text(f"""model:\n  default: business-e2e-model\n  provider: custom\n  base_url: http://127.0.0.1:{provider.server_port}/v1\n  api_key: e2e\n  api_mode: chat_completions\nplugins:\n  enabled:\n    - astra-runtime\nplatform_toolsets:\n  cli:\n    - hermes-cli\nknown_plugin_toolsets:\n  cli:\n    - astra_capabilities\n    - astra_runtime\n    - astra_business\n""")
    env = {**previous, "HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "true", "ASTRA_RUNTIME_ENDPOINT": f"http://127.0.0.1:{runtime_server.server_port}", "ASTRA_RUNTIME_PLUGIN_AUTH": "runtime-e2e-auth", "PYTHONPATH": str(ROOT)}
    for key in ("ASTRA_BUSINESS_SANDBOX_ENDPOINT", "ASTRA_BUSINESS_SANDBOX_TOKEN", "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN", "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN"): env.pop(key, None)
    try:
        completed = subprocess.run(
            [str(HERMES.parent / "python"), "-m", "astra.hermes_adapter.launch", str(HERMES),
             "chat", "-q", "查 cust-s12-s13-001 最近一笔订单状态"],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=45, check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "order-s12-s13-001" in completed.stdout
        assert "delivered" in completed.stdout
        assert "unknown toolset" not in completed.stderr.lower()
        schema_request = next(request for request in _BusinessE2EProvider.requests if request.get("tools"))
        final_schema = {
            tool["function"]["name"]: tool["function"]
            for tool in schema_request["tools"]
            if tool.get("type") == "function"
        }
        assert set(name for name in final_schema if name.startswith("astra_")) == {
            "astra_get_latest_customer_order"
        }
        # This is the actual provider-facing CLI trace: the model receives the
        # semantic capability on startup, invokes it once, then immediately
        # produces its final answer.  No legacy Runtime protocol is exposed.
        trace = [
            call["function"]["name"]
            for request in _BusinessE2EProvider.requests
            for message in request.get("messages", [])
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        ]
        assert trace == ["astra_get_latest_customer_order"]
        conversation_requests = [
            request for request in _BusinessE2EProvider.requests
            if isinstance(request.get("messages"), list)
        ]
        tool_result_turn = next(
            request for request in conversation_requests
            if any(message.get("role") == "tool" for message in request["messages"])
        )
        assert any(
            message.get("role") == "tool"
            for message in tool_result_turn["messages"]
        )
        # Segment the acceptance timing. Runtime timings are server-side; the
        # provider timestamps bracket the model decisions. This intentionally
        # diagnoses protocol/provider overhead without asserting performance
        # thresholds in a network-dependent test.
        assert len(_BusinessE2EProvider.request_times) >= 2
        assert "capability" in {item["operation"] for item in _TimedRuntimeHandler.timings}
        timing_report = {
            "t1_provider_first_decision_s": _BusinessE2EProvider.request_times[0],
            "t2_broker_runtime_s": next(item["elapsed_s"] for item in _TimedRuntimeHandler.timings if item["operation"] == "capability"),
            "t3_provider_final_decision_s": _BusinessE2EProvider.request_times[-1],
        }
        assert all(value >= 0 for value in timing_report.values())
        # Runtime mechanics never reach the model-facing broker schema/result.
        serialized_tools = repr(_BusinessE2EProvider.requests).lower()
        for forbidden in ("schema_hash", "capability_ref", "resolved_tools", "execution_id", "lease_token"):
            assert forbidden not in serialized_tools
        assert any(m.get("role") == "tool" for r in _BusinessE2EProvider.requests for m in r.get("messages", []))
        tool_results = [json.loads(m["content"]) for r in _BusinessE2EProvider.requests for m in r.get("messages", []) if m.get("role") == "tool"]
        broker_result = next(item for item in tool_results if item.get("order_id"))
        assert broker_result["order_id"] == "order-s12-s13-001"
        assert broker_result["status"] == "delivered"
        assert broker_result["receipt_ref"]
        assert broker_result["evidence_ref"]["type"] == "execution_receipt"
    finally:
        runtime_server.shutdown(); provider.shutdown(); sandbox.shutdown(); RuntimeHandler.service.close(); os.environ.clear(); os.environ.update(previous)


def _unused_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.mark.skipif(
    not HERMES.exists() or not Path("/usr/bin/sandbox-exec").exists(),
    reason="Hermes or macOS sandbox-exec unavailable",
)
def test_launcher_parent_interactive_session_binds_and_invokes_astra_capability(tmp_path: Path):
    """Formal launcher E2E: one PTY, one parent Hermes process, no chat -q."""
    _BusinessE2EProvider.requests = []
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _BusinessE2EProvider)
    provider_thread = threading.Thread(target=provider.serve_forever, daemon=True)
    provider_thread.start()
    home = tmp_path / "hermes-home"
    home.mkdir()
    config_path = home / "config.yaml"
    config_path.write_text(
        f"""model:
  default: business-e2e-model
  provider: custom
  base_url: http://127.0.0.1:{provider.server_port}/v1
  api_key: e2e
  api_mode: chat_completions
plugins:
  enabled:
    - astra-runtime
platform_toolsets:
  cli:
    - hermes-cli
known_plugin_toolsets:
  cli:
    - astra_capabilities
    - astra_runtime
    - astra_business
""",
        encoding="utf-8",
    )
    runtime_port = _unused_port()
    sandbox_port = _unused_port()
    authority_dir = tmp_path / "runtime-owned-business"
    authority_dir.mkdir()
    audit_file = tmp_path / "parent-provider-tools.json"
    env = {
        **os.environ,
        "HERMES_HOME": str(home),
        "HERMES_BIN": str(HERMES),
        "PYTHON_BIN": str(HERMES.parent / "python"),
        "ASTRA_RUNTIME_PORT": str(runtime_port),
        "ASTRA_BUSINESS_SANDBOX_PORT": str(sandbox_port),
        "ASTRA_BUSINESS_SANDBOX_DATABASE": str(authority_dir / "business.sqlite3"),
        "ASTRA_DATABASE": str(tmp_path / "astra.sqlite3"),
        "ASTRA_RUNTIME_PID_FILE": str(tmp_path / "runtime.pid"),
        "ASTRA_RUNTIME_LOG_FILE": str(tmp_path / "runtime.log"),
        "ASTRA_BUSINESS_SANDBOX_LOG_FILE": str(tmp_path / "sandbox.log"),
        "ASTRA_PARENT_SESSION_AUDIT_FILE": str(audit_file),
        "TERM": "xterm-256color",
        "NO_COLOR": "1",
    }
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [str(ROOT / "scripts" / "start_astra_cli.sh")],
        cwd=ROOT,
        env=env,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave)
    transcript = bytearray()
    query_sent = False
    exit_sent = False
    cpr_replies = 0
    deadline = time.monotonic() + 60
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.25)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                    transcript.extend(chunk)
                    # prompt_toolkit asks a real terminal for cursor position
                    # before presenting the input buffer. A raw PTY has no
                    # emulator, so provide the standard CPR response.
                    queries = chunk.count(b"\x1b[6n")
                    for _ in range(queries):
                        os.write(master, b"\x1b[1;1R")
                    cpr_replies += queries
                except OSError:
                    break
            rendered = transcript.decode("utf-8", errors="replace")
            if (
                not query_sent
                and "Welcome to Hermes Agent!" in rendered
                and "❯" in rendered
                and cpr_replies
            ):
                os.write(master, "查 cust-s12-s13-001 最近一笔订单状态\n".encode())
                query_sent = True
            if query_sent and not exit_sent and "order-s12-s13-001" in rendered and "delivered" in rendered:
                os.write(master, b"/exit\n")
                exit_sent = True
            if exit_sent and process.poll() is not None:
                break
            if process.poll() is not None and not exit_sent:
                break
        rendered = transcript.decode("utf-8", errors="replace")
        assert query_sent, rendered
        assert "order-s12-s13-001" in rendered and "delivered" in rendered, rendered
        assert process.poll() == 0, rendered

        schema_request = next(request for request in _BusinessE2EProvider.requests if request.get("tools"))
        tool_names = {
            tool["function"]["name"] for tool in schema_request["tools"]
            if tool.get("type") == "function"
        }
        assert "astra_get_latest_customer_order" in tool_names
        calls = [
            call["function"]["name"]
            for request in _BusinessE2EProvider.requests
            for message in request.get("messages", [])
            if message.get("role") == "assistant"
            for call in message.get("tool_calls", [])
        ]
        assert calls == ["astra_get_latest_customer_order"]
        assert not {"terminal", "skill_view", "search_files"} & set(calls)
        audit = json.loads(audit_file.read_text(encoding="utf-8"))
        assert audit["source"] == "hermes_parent_session_pre_api_request"
        assert audit["astra_capability_bound"] is True
        assert audit["tool_call_names"] == ["astra_get_latest_customer_order"]
        assert audit["session_id"] and not audit["session_id"].startswith("preflight:")

        # The launcher did not manufacture a successful persisted config.
        persisted = config_path.read_text(encoding="utf-8")
        assert "    - astra_capabilities" not in persisted.split("known_plugin_toolsets:")[0]
        assert "    - astra_runtime" in persisted
        assert "    - astra_business" in persisted
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
        else:
            # Background Runtime/Sandbox inherit the launcher's process group.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        os.close(master)
        provider.shutdown()
        provider_thread.join()


class _ObservationProvider(_BusinessE2EProvider):
    """Deterministically asks for one governed read then one Skill view."""
    def do_POST(self) -> None:  # noqa: N802
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        type(self).requests.append(request)
        tool_count = sum(message.get("role") == "tool" for message in request.get("messages", []))
        if tool_count >= 2:
            self._send_stream({"role": "assistant", "content": "done"}, "stop")
            return
        name, arguments = (("astra_get_latest_customer_order", {"customer_id": "cust-s12-s13-001"})
                           if tool_count == 0 else ("skill_view", {"name": "observation-test"}))
        self._send_stream({"role": "assistant", "tool_calls": [{"index": 0, "id": f"observation-{tool_count}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}, "tool_calls")


@pytest.mark.skipif(not HERMES.exists(), reason="Hermes test executable is unavailable")
def test_parent_session_skill_view_is_observed_and_correlated(tmp_path: Path):
    _ObservationProvider.requests = []
    sandbox_store = SandboxStore(tmp_path / "business.sqlite3"); sandbox_store.seed()
    sandbox = SandboxServer(("127.0.0.1", 0), sandbox_store, "sandbox-e2e-token")
    provider = ThreadingHTTPServer(("127.0.0.1", 0), _ObservationProvider)
    threading.Thread(target=sandbox.serve_forever, daemon=True).start(); threading.Thread(target=provider.serve_forever, daemon=True).start()
    database = tmp_path / "astra.sqlite3"; previous = dict(os.environ)
    os.environ.update({"ASTRA_DATABASE": str(database), "ASTRA_BUSINESS_SANDBOX_ENDPOINT": f"http://127.0.0.1:{sandbox.server_address[1]}", "ASTRA_BUSINESS_SANDBOX_TOKEN": "sandbox-e2e-token", "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN": "commerce.e2e", "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN": "support.e2e", "ASTRA_RUNTIME_PLUGIN_AUTH": "runtime-e2e-auth"})
    RuntimeHandler.service = build_runtime_service_from_environment()
    runtime_server = ThreadingHTTPServer(("127.0.0.1", 0), RuntimeHandler); threading.Thread(target=runtime_server.serve_forever, daemon=True).start()
    home = tmp_path / "hermes-home"; skill = home / "skills" / "test" / "observation-test"; skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: observation-test\ndescription: test\n---\n\ncontent must not persist", encoding="utf-8")
    (home / "config.yaml").write_text(f"model:\n  default: business-e2e-model\n  provider: custom\n  base_url: http://127.0.0.1:{provider.server_port}/v1\n  api_key: e2e\n  api_mode: chat_completions\nplugins:\n  enabled:\n    - astra-runtime\n", encoding="utf-8")
    env = {**previous, "HERMES_HOME": str(home), "HERMES_ENABLE_PROJECT_PLUGINS": "true", "ASTRA_RUNTIME_ENDPOINT": f"http://127.0.0.1:{runtime_server.server_port}", "ASTRA_RUNTIME_PLUGIN_AUTH": "runtime-e2e-auth", "PYTHONPATH": str(ROOT)}
    try:
        completed = subprocess.run([str(HERMES.parent / "python"), "-m", "astra.hermes_adapter.launch", str(HERMES), "chat", "-q", "check order"], cwd=ROOT, env=env, capture_output=True, text=True, timeout=45, check=False)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        store = AstraStore(database)
        try:
            row = store.query_one("SELECT * FROM artifact_action_observations WHERE action = 'view'")
            assert row is not None and row["correlation_status"] == "bound"
            assert row["task_id"] and row["attempt_id"] and row["execution_id"]
            assert row["session_id"] and row["artifact_ref"]
            assert "content must not persist" not in row["metadata_json"]
        finally: store.close()
    finally:
        runtime_server.shutdown(); provider.shutdown(); sandbox.shutdown(); RuntimeHandler.service.close(); os.environ.clear(); os.environ.update(previous)
