"""Public Hermes plugin: business vocabulary only, with no Runtime mechanics."""

from __future__ import annotations

import json
import os
import contextvars
import threading
from pathlib import Path
from uuid import uuid4
from collections.abc import Mapping
from typing import Any
from urllib.request import Request, urlopen

from ..capability_registry import HERMES_CAPABILITY_TOOLSET, load_capabilities
from ..artifact_observations import from_post_tool_call

_transport_credential_cache: str | None = None
_current_session: contextvars.ContextVar[str | None] = contextvars.ContextVar("astra_hermes_session", default=None)
_session_guard = threading.RLock()
_active_session_id: str | None = None
_session_ambiguous = False


def _clear_session() -> None:
    global _active_session_id, _session_ambiguous
    with _session_guard:
        _active_session_id = None
        _session_ambiguous = False
        _current_session.set(None)


def _observe_cli_session(session_id: str, *, new_session: bool = False) -> bool:
    """Permit exactly one unambiguous CLI session in this plugin process."""
    global _active_session_id, _session_ambiguous
    sid = str(session_id).strip()
    if not sid:
        _clear_session()
        return False
    with _session_guard:
        if new_session:
            if _active_session_id not in (None, sid):
                _clear_session()
                _session_ambiguous = True
                return False
            _session_ambiguous = False
        elif _active_session_id not in (None, sid):
            _clear_session()
            _session_ambiguous = True
            return False
        if _session_ambiguous:
            _current_session.set(None)
            return False
        _active_session_id = sid
        _current_session.set(sid)
        return True


def _bindable_session() -> str | None:
    with _session_guard:
        if _session_ambiguous or _active_session_id is None:
            return None
        current = _current_session.get()
        return _active_session_id if current == _active_session_id else None


def _provider_tool_names(request: Mapping[str, Any]) -> list[str]:
    body = request.get("body", request)
    tools = body.get("tools", []) if isinstance(body, Mapping) else []
    return sorted({
        str(tool.get("function", {}).get("name", ""))
        for tool in tools
        if isinstance(tool, Mapping)
        and isinstance(tool.get("function"), Mapping)
        and tool.get("function", {}).get("name")
    })


def _provider_tool_call_names(request: Mapping[str, Any]) -> list[str]:
    body = request.get("body", request)
    messages = body.get("messages", []) if isinstance(body, Mapping) else []
    return [
        str(call.get("function", {}).get("name", ""))
        for message in messages
        if isinstance(message, Mapping) and message.get("role") == "assistant"
        for call in message.get("tool_calls", [])
        if isinstance(call, Mapping)
        and isinstance(call.get("function"), Mapping)
        and call.get("function", {}).get("name")
    ]


def _audit_provider_request(*, session_id: str = "", request: Mapping[str, Any] | None = None,
                            api_call_count: int = 0, **_: Any) -> None:
    """Persist the real parent session's provider-facing tool projection."""
    audit_file = os.environ.get("ASTRA_PARENT_SESSION_AUDIT_FILE", "").strip()
    if not audit_file or not isinstance(request, Mapping):
        return
    tool_names = _provider_tool_names(request)
    payload = {
        "session_id": session_id,
        "api_call_count": api_call_count,
        "tool_names": tool_names,
        "tool_call_names": _provider_tool_call_names(request),
        "astra_capability_bound": "astra_get_latest_customer_order" in tool_names,
        "source": "hermes_parent_session_pre_api_request",
    }
    path = Path(audit_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _error(error_type: str, message: str) -> str:
    return json.dumps({"ok": False, "error": {"type": error_type, "message": message}}, ensure_ascii=False)


def _endpoint() -> str:
    endpoint = os.environ.get("ASTRA_RUNTIME_ENDPOINT", "").strip().rstrip("/")
    if not endpoint:
        raise RuntimeError("Astra Runtime endpoint is not configured")
    return endpoint


def _transport_credential() -> str:
    """Machine credential, intentionally absent from every tool contract/result."""
    credential = _transport_credential_cache
    if not credential:
        raise RuntimeError("Astra Runtime plugin authentication is not configured")
    return credential


def _capture_transport_credential() -> None:
    """Remove the credential before Hermes can spawn terminal children.

    Plugin registration runs in Hermes' process.  Keeping the value in
    ``os.environ`` would make it inheritable by terminal subprocesses, so this
    is deliberately a one-way transfer to plugin memory.
    """
    global _transport_credential_cache
    if _transport_credential_cache is None:
        _transport_credential_cache = os.environ.pop("ASTRA_RUNTIME_PLUGIN_AUTH", "").strip()
    if not _transport_credential_cache:
        raise RuntimeError("Astra Runtime plugin authentication is not configured")


def _call(operation: str, args: Mapping[str, Any], tool_name: str | None = None) -> str:
    payload: dict[str, Any] = {"operation": operation, "args": dict(args)}
    if tool_name:
        payload["tool_name"] = tool_name
    request = Request(_endpoint() + "/v1/plugin", data=json.dumps(payload).encode(),
                      headers={"Content-Type": "application/json",
                               "X-Astra-Plugin-Auth": _transport_credential()}, method="POST")
    with urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode())
    if not isinstance(body, dict) or not isinstance(body.get("result"), str):
        raise RuntimeError("invalid_astra_runtime_service_response")
    return body["result"]


def _handler(operation: str, tool_name: str | None = None):
    def handler(args: Mapping[str, Any], **_: Any) -> str:
        try:
            if operation == "capability":
                args = {**dict(args), "capability_name": str(tool_name or ""),
                        "_hermes_session_id": _bindable_session() or ""}
            if operation == "submit":
                # Re-entry is idempotent: reuse the Task already correlated to
                # this Hermes session instead of submitting a second Task.
                session_id = _bindable_session()
                if session_id:
                    existing = json.loads(_call("correlation_lookup", {"hermes_session_id": session_id}))
                    bound = existing.get("correlation") if isinstance(existing, dict) else None
                    if isinstance(bound, dict) and bound.get("astra_task_id"):
                        current_task = str(bound["astra_task_id"])
                        status = json.loads(_call("status", {"task_id": current_task}))
                        business_status = status.get("business_status") if isinstance(status, dict) else None
                        if business_status in {"pending", "running", "waiting_input", "waiting_approval"}:
                            return json.dumps({**status, "reused": True}, ensure_ascii=False)
                        _call("correlation_release", {"hermes_session_id": session_id})
                # Identity remains opaque to Hermes; Runtime keeps its existing
                # command/idempotency semantics for the supplied values.
                args = {**dict(args), "command_id": "hermes-command:" + str(uuid4()),
                        "task_id": "hermes-task:" + str(uuid4())}
            elif operation == "business" and not str(args.get("task_id", "")).strip():
                session_id = _bindable_session()
                if session_id:
                    correlation = json.loads(_call("correlation_lookup", {"hermes_session_id": session_id}))
                    bound = correlation.get("correlation") if isinstance(correlation, dict) else None
                    if isinstance(bound, dict) and bound.get("astra_task_id"):
                        args = {**dict(args), "task_id": bound["astra_task_id"]}
            result = _call(operation, args, tool_name)
            if operation == "submit":
                session_id = _bindable_session()
                try:
                    payload = json.loads(result)
                    task_id = payload.get("task_id") if isinstance(payload, dict) and payload.get("ok") else None
                    if session_id and task_id:
                        correlation = json.loads(_call("correlation_bind", {"hermes_session_id": session_id, "astra_task_id": task_id, "source": "astra_submit_task"}))
                        if not isinstance(correlation, dict) or not correlation.get("ok"):
                            raise RuntimeError("astra_task_submitted_but_session_correlation_rejected")
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            return result
        except (OSError, ValueError, RuntimeError) as exc:
            return _error("astra_runtime_unavailable", str(exc))
    return handler


def _command_probe(_raw_args: str) -> str:
    """Small public-command spike for validating same-session re-entry.

    Hermes plugin commands receive only their raw argument string.  The only
    supported way for this adapter to associate a command with a CLI session is
    the session context captured by the public lifecycle hooks above.  This
    probe deliberately performs a read-only Runtime lookup and returns text;
    it does not inject a prompt, resume execution, claim work, or select tools.
    """
    session_id = _bindable_session()
    if not session_id:
        return _error("astra_command_unbound", "Astra command is not bound to an unambiguous CLI session")
    try:
        correlation = json.loads(_call("correlation_lookup", {"hermes_session_id": session_id}))
        return json.dumps({
            "ok": True,
            "session_id": session_id,
            "correlation": correlation.get("correlation") if isinstance(correlation, dict) else None,
            "continuation": "command_return_only",
        }, ensure_ascii=False)
    except (OSError, ValueError, RuntimeError) as exc:
        return _error("astra_runtime_unavailable", str(exc))


def _schema(name: str, description: str, properties: Mapping[str, Any], required: list[str]) -> Mapping[str, Any]:
    return {"name": name, "description": description, "parameters": {
        "type": "object", "properties": dict(properties), "required": required,
        "additionalProperties": False,
    }}


def register(ctx: Any) -> None:
    """Register documented plugin tools; all authority lives in Runtime service."""
    _capture_transport_credential()
    def on_session_start(**kwargs: Any) -> None:
        if str(kwargs.get("platform", "cli")).strip().lower() not in ("", "cli"):
            _clear_session()
            return
        sid = str(kwargs.get("session_id", "")).strip()
        if _observe_cli_session(sid, new_session=True):
            _call("correlation_lookup", {"hermes_session_id": sid})

    def on_pre_llm_call(**kwargs: Any) -> None:
        if str(kwargs.get("platform", "cli")).strip().lower() not in ("", "cli"):
            _clear_session()
            return
        sid = str(kwargs.get("session_id", "")).strip()
        if _observe_cli_session(sid):
            _call("correlation_lookup", {"hermes_session_id": sid})

    def clear_session(**kwargs: Any) -> None:
        _clear_session()

    def on_post_tool_call(**kwargs: Any) -> None:
        """Forward public, content-free Artifact Action telemetry fail-open."""
        try:
            observation = from_post_tool_call(kwargs)
            if observation is not None:
                _call("artifact_action_observation", observation.__dict__)
        except Exception:
            # Observer failures must never affect Hermes tool execution.
            return None

    # ``register_hook`` is part of Hermes' public PluginContext.  The guard
    # keeps schema-only consumers backward-compatible without changing Hermes.
    if hasattr(ctx, "register_hook"):
        ctx.register_hook("on_session_start", on_session_start)
        ctx.register_hook("pre_llm_call", on_pre_llm_call)
        ctx.register_hook("on_session_reset", clear_session)
        ctx.register_hook("on_session_finalize", clear_session)
        ctx.register_hook("pre_api_request", _audit_provider_request)
        ctx.register_hook("post_tool_call", on_post_tool_call)
    # Stage 2B-0 public API spike.  Hermes command dispatch returns this text
    # to the caller; it does not start another model turn automatically.
    if hasattr(ctx, "register_command"):
        ctx.register_command(
            "astra-probe-session",
            _command_probe,
            description="Probe Astra command session binding (Stage 2B-0)",
        )
    # Product-facing surface: semantic capabilities only.  The retained
    # submit/status/business functions above are the Broker's diagnostic/test
    # protocol and are deliberately not registered in the Hermes model schema.
    for capability in load_capabilities():
        ctx.register_tool(name=capability.name, toolset=HERMES_CAPABILITY_TOOLSET, schema=_schema(
            capability.name,
            capability.description + " Astra internally governs task execution, authorization, evidence, and receipt creation; answer only from this Tool Result.",
            capability.input_schema["properties"],
            list(capability.input_schema["required"])), handler=_handler("capability", capability.name))
