"""Phase 1 compatibility probe for Hermes Agent 0.18.2.

The probe deliberately uses a scripted provider response because the project
workspace has no model credentials. Everything after the provider boundary is
the real Hermes implementation: plugin discovery, tool schema exposure, the
agent loop, pre-tool constraints, tool dispatch, tool-result feedback,
observation hooks, session persistence, and final result assembly.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock


WORKSPACE_ROOT = Path(__file__).resolve().parents[1]
HERMES_ROOT = WORKSPACE_ROOT / "hermes-agent-main"


def _write_probe_plugin(hermes_home: Path) -> Path:
    plugin_dir = hermes_home / "plugins" / "astra_phase1_probe"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        "name: astra_phase1_probe\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        '''import json
import os
from pathlib import Path


EVENT_LOG = Path(os.environ["ASTRA_PHASE1_EVENT_LOG"])


def _emit(event, **payload):
    record = {"event": event, **payload}
    with EVENT_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\\n")


def _lookup(args, **kwargs):
    _emit("tool_handler", args=args, task_id=kwargs.get("task_id"))
    return json.dumps({"ok": True, "key": args["key"], "value": 42})


def _pre_llm_call(**kwargs):
    _emit("pre_llm_call", turn_id=kwargs.get("turn_id"))
    return {"context": "ASTRA_FEEDBACK_CHANNEL_READY"}


def _pre_api_request(**kwargs):
    body = (kwargs.get("request") or {}).get("body") or {}
    tools = body.get("tools") or []
    names = [
        item.get("function", {}).get("name")
        for item in tools
        if isinstance(item, dict)
    ]
    _emit(
        "pre_api_request",
        api_request_id=kwargs.get("api_request_id"),
        api_call_count=kwargs.get("api_call_count"),
        tool_names=names,
    )


def _post_api_request(**kwargs):
    _emit(
        "post_api_request",
        api_request_id=kwargs.get("api_request_id"),
        finish_reason=kwargs.get("finish_reason"),
        usage=kwargs.get("usage"),
    )


def _pre_tool_call(**kwargs):
    args = kwargs.get("args") or {}
    _emit(
        "pre_tool_call",
        tool_call_id=kwargs.get("tool_call_id"),
        tool_name=kwargs.get("tool_name"),
        args=args,
    )
    if args.get("key") == "blocked":
        return {
            "action": "block",
            "message": "Blocked by Phase 1 deterministic policy",
        }
    return None


def _post_tool_call(**kwargs):
    _emit(
        "post_tool_call",
        tool_call_id=kwargs.get("tool_call_id"),
        tool_name=kwargs.get("tool_name"),
        status=kwargs.get("status"),
        error_type=kwargs.get("error_type"),
        result=kwargs.get("result"),
    )


def _transform_tool_result(**kwargs):
    result = json.loads(kwargs["result"])
    result["astra_feedback"] = {
        "type": "ToolResultEvidence",
        "message": "controlled tool result accepted",
    }
    _emit("transform_tool_result", tool_call_id=kwargs.get("tool_call_id"))
    return json.dumps(result, ensure_ascii=False)


def _post_llm_call(**kwargs):
    _emit("post_llm_call", assistant_response=kwargs.get("assistant_response"))


def _on_session_start(**kwargs):
    _emit("on_session_start", session_id=kwargs.get("session_id"))


def _on_session_end(**kwargs):
    _emit(
        "on_session_end",
        session_id=kwargs.get("session_id"),
        completed=kwargs.get("completed"),
        interrupted=kwargs.get("interrupted"),
    )


def register(ctx):
    ctx.register_tool(
        name="astra_lookup",
        toolset="astra_phase1_probe",
        schema={
            "name": "astra_lookup",
            "description": "Return a deterministic value for a test key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
                "additionalProperties": False,
            },
        },
        handler=_lookup,
    )
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_api_request", _pre_api_request)
    ctx.register_hook("post_api_request", _post_api_request)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("transform_tool_result", _transform_tool_result)
    ctx.register_hook("post_llm_call", _post_llm_call)
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
''',
        encoding="utf-8",
    )
    return plugin_dir


def _tool_call(call_id: str, key: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(
            name="astra_lookup",
            arguments=json.dumps({"key": key}),
        ),
    )


def _response(
    *,
    content: str = "",
    finish_reason: str = "stop",
    tool_calls: list[SimpleNamespace] | None = None,
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    usage = SimpleNamespace(
        prompt_tokens=20,
        completion_tokens=5,
        total_tokens=25,
        prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        completion_tokens_details=SimpleNamespace(reasoning_tokens=0),
    )
    return SimpleNamespace(
        id="scripted-response",
        choices=[choice],
        model="astra-scripted-provider",
        usage=usage,
    )


def _scripted_provider(**request):
    messages = request["messages"]
    tool_names = {
        item["function"]["name"]
        for item in request.get("tools") or []
    }
    assert "astra_lookup" in tool_names
    assert "ASTRA_TASK_CONTRACT: tool astra_lookup is allowed" in messages[0]["content"]
    assert any(
        "ASTRA_FEEDBACK_CHANNEL_READY" in str(message.get("content", ""))
        for message in messages
        if message.get("role") == "user"
    )

    tool_results = [message for message in messages if message.get("role") == "tool"]
    if not tool_results:
        return _response(
            finish_reason="tool_calls",
            tool_calls=[_tool_call("call-blocked", "blocked")],
        )

    latest = str(tool_results[-1].get("content", ""))
    if "Blocked by Phase 1 deterministic policy" in latest:
        return _response(
            finish_reason="tool_calls",
            tool_calls=[_tool_call("call-allowed", "answer")],
        )

    parsed = json.loads(latest)
    assert parsed["value"] == 42
    assert parsed["astra_feedback"]["type"] == "ToolResultEvidence"
    return _response(
        content="Lookup complete: value=42; structured feedback observed.",
        finish_reason="stop",
    )


def test_real_hermes_loop_with_controlled_tool_and_boundaries(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    event_log = tmp_path / "events.jsonl"

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ASTRA_PHASE1_EVENT_LOG", str(event_log))
    (hermes_home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - astra_phase1_probe\n",
        encoding="utf-8",
    )
    _write_probe_plugin(hermes_home)

    sys.path.insert(0, str(HERMES_ROOT))
    try:
        import hermes_cli.plugins as plugins

        plugins._plugin_manager = plugins.PluginManager()

        from hermes_state import SessionDB
        from model_tools import get_tool_definitions
        from run_agent import AIAgent

        tool_defs = get_tool_definitions(
            enabled_toolsets=["astra_phase1_probe"],
            quiet_mode=True,
        )
        assert [item["function"]["name"] for item in tool_defs] == ["astra_lookup"]

        session_db = SessionDB()
        agent = AIAgent(
            base_url="http://127.0.0.1:9/v1",
            api_key="phase1-dummy-key",
            provider="custom",
            api_mode="chat_completions",
            model="astra-scripted-provider",
            max_iterations=6,
            tool_delay=0,
            enabled_toolsets=["astra_phase1_probe"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            session_db=session_db,
            session_id="phase1-compatibility-session",
            ephemeral_system_prompt=(
                "ASTRA_TASK_CONTRACT: tool astra_lookup is allowed; "
                "all other business tools are denied."
            ),
        )

        client = MagicMock()
        client.chat.completions.create.side_effect = _scripted_provider
        agent.client = client
        agent._disable_streaming = True

        result = agent.run_conversation(
            "Use the available controlled lookup tool and report its value.",
            task_id="phase1-task",
        )

        assert result["completed"] is True
        assert result["interrupted"] is False
        assert result["failed"] is False
        assert result["api_calls"] == 3
        assert result["final_response"] == (
            "Lookup complete: value=42; structured feedback observed."
        )
        assert result["session_id"] == "phase1-compatibility-session"
        assert result["total_tokens"] == 75

        persisted = session_db.get_messages("phase1-compatibility-session")
        persisted_roles = [message["role"] for message in persisted]
        assert persisted_roles == [
            "user",
            "assistant",
            "tool",
            "assistant",
            "tool",
            "assistant",
        ]

        events = [
            json.loads(line)
            for line in event_log.read_text(encoding="utf-8").splitlines()
        ]
        event_names = [event["event"] for event in events]
        assert event_names.count("pre_api_request") == 3
        assert event_names.count("post_api_request") == 3
        assert event_names.count("pre_tool_call") == 2
        assert event_names.count("post_tool_call") == 2
        assert event_names.count("tool_handler") == 1
        assert event_names.count("transform_tool_result") == 1
        assert event_names.count("post_llm_call") == 1
        assert event_names.count("on_session_start") == 1
        assert event_names.count("on_session_end") == 1

        post_tool_events = [
            event for event in events if event["event"] == "post_tool_call"
        ]
        assert [event["status"] for event in post_tool_events] == [
            "blocked",
            "ok",
        ]
    finally:
        sys.path.remove(str(HERMES_ROOT))
