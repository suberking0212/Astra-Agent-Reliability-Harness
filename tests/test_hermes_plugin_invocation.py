"""Black-box verification for the supported Hermes public-plugin boundary."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from astra.storage import AstraStore


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
    environment = {
        **os.environ,
        "HERMES_HOME": str(hermes_home),
        "HERMES_ENABLE_PROJECT_PLUGINS": "true",
        "ASTRA_DATABASE": str(database),
        "ASTRA_BUSINESS_SANDBOX_ENDPOINT": "http://127.0.0.1:8765",
        "ASTRA_BUSINESS_SANDBOX_TOKEN": "plugin-test-token",
        "ASTRA_BUSINESS_COMMERCE_AUTHORITY_DOMAIN": "commerce.plugin-test",
        "ASTRA_BUSINESS_SUPPORT_AUTHORITY_DOMAIN": "support.plugin-test",
        "PYTHONPATH": str(ROOT),
    }
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
    tool_results_by_call = {
        message["tool_call_id"]: json.loads(message["content"])
        for request in _ScriptedProvider.requests
        for message in request.get("messages", [])
        if message.get("role") == "tool"
    }
    tool_results = [tool_results_by_call[f"call-{index}"] for index in range(len(task_ids))]
    assert tool_results == [
        {"task_id": task_id, "runtime_status": state, "runtime_status_zh": localized}
        for (state, localized), task_id in zip(STATES.items(), task_ids, strict=True)
    ]
