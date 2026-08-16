from __future__ import annotations

import json

from astra.hermes_adapter import plugin


class _Context:
    def __init__(self) -> None:
        self.commands = {}
        self.hooks = {}

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_command(self, name, handler, **kwargs):
        self.commands[name] = (handler, kwargs)

    def register_tool(self, **_kwargs):
        pass


def test_stage2b0_command_sees_hook_bound_session_and_only_returns_text(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    monkeypatch.setattr(
        plugin,
        "_call",
        lambda operation, args, tool_name=None: calls.append((operation, dict(args)))
        or json.dumps({"ok": True, "correlation": {"astra_task_id": "task-1"}}),
    )
    ctx = _Context()
    plugin.register(ctx)

    assert "astra-probe-session" in ctx.commands
    ctx.hooks["on_session_start"](session_id="cli-1", platform="cli")
    result = json.loads(ctx.commands["astra-probe-session"][0]("ignored raw args"))

    assert result == {
        "ok": True,
        "session_id": "cli-1",
        "correlation": {"astra_task_id": "task-1"},
        "continuation": "command_return_only",
    }
    assert calls[-1] == ("correlation_lookup", {"hermes_session_id": "cli-1"})


def test_stage2b0_command_fails_closed_after_reset_and_finalize(monkeypatch):
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    monkeypatch.setattr(plugin, "_call", lambda *args, **kwargs: json.dumps({"ok": True}))
    ctx = _Context()
    plugin.register(ctx)
    command = ctx.commands["astra-probe-session"][0]
    ctx.hooks["on_session_start"](session_id="cli-1", platform="cli")

    ctx.hooks["on_session_reset"]()
    reset_result = json.loads(command(""))
    ctx.hooks["on_session_start"](session_id="cli-1", platform="cli")
    ctx.hooks["on_session_finalize"]()
    finalize_result = json.loads(command(""))

    assert reset_result["ok"] is False
    assert reset_result["error"]["type"] == "astra_command_unbound"
    assert finalize_result["ok"] is False
    assert finalize_result["error"]["type"] == "astra_command_unbound"


def test_stage2b0_command_is_ambiguous_when_hook_observes_two_cli_sessions(monkeypatch):
    monkeypatch.setattr(plugin, "_transport_credential_cache", "auth")
    monkeypatch.setattr(plugin, "_call", lambda *args, **kwargs: json.dumps({"ok": True}))
    ctx = _Context()
    plugin.register(ctx)
    command = ctx.commands["astra-probe-session"][0]
    ctx.hooks["on_session_start"](session_id="cli-a", platform="cli")
    ctx.hooks["pre_llm_call"](session_id="cli-b", platform="cli")

    result = json.loads(command(""))
    assert result["ok"] is False
    assert result["error"]["type"] == "astra_command_unbound"
