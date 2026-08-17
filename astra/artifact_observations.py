"""Content-free Hermes Artifact Action Observation parsing and persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import uuid4


@dataclass(frozen=True)
class ArtifactActionObservation:
    event_id: str
    session_id: str
    hermes_task_id: str
    tool_call_id: str
    turn_id: str
    api_request_id: str
    artifact_type: str
    artifact_ref: str | None
    action: str
    status: str
    timestamp: str
    metadata: Mapping[str, str]


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def from_post_tool_call(payload: Mapping[str, Any]) -> ArtifactActionObservation | None:
    tool_name = str(payload.get("tool_name", ""))
    args = _mapping(payload.get("args"))
    result: Mapping[str, Any] = {}
    try:
        result = _mapping(json.loads(payload.get("result", "")))
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    action = ""
    artifact_type = ""
    artifact_ref: str | None = None
    metadata: dict[str, str] = {}
    if tool_name == "skills_list":
        artifact_type, action = "skill", "list"
        if args.get("category"):
            metadata["category"] = str(args["category"])
    elif tool_name == "skill_view":
        artifact_type, action = "skill", "view"
        if result.get("success") is True:
            for key in ("name", "path", "skill_dir"):
                if result.get(key):
                    metadata[key] = str(result[key])
            artifact_ref = metadata.get("path") or metadata.get("name")
    elif tool_name == "skill_manage":
        requested = str(args.get("action", "")).lower()
        actions = {"create": "create", "update": "update", "delete": "delete", "remove": "delete"}
        if requested in actions and result.get("success") is True:
            artifact_type, action = "skill", actions[requested]
            if args.get("name"):
                artifact_ref = str(args["name"])
    elif tool_name == "memory":
        requested = str(args.get("action", "")).lower()
        actions = {"search": "search", "read": "read", "add": "write", "replace": "write", "write": "write", "remove": "delete", "delete": "delete"}
        if requested in actions:
            artifact_type, action = "memory", actions[requested]
            if args.get("target"):
                metadata["target"] = str(args["target"])
    if not action:
        return None
    tool_call_id = str(payload.get("tool_call_id", "")).strip()
    if not tool_call_id:
        return None
    return ArtifactActionObservation(
        event_id="artifact-action:" + tool_call_id,
        session_id=str(payload.get("session_id", "")).strip(),
        hermes_task_id=str(payload.get("task_id", "")).strip(),
        tool_call_id=tool_call_id,
        turn_id=str(payload.get("turn_id", "")).strip(),
        api_request_id=str(payload.get("api_request_id", "")).strip(),
        artifact_type=artifact_type, artifact_ref=artifact_ref, action=action,
        status=str(payload.get("status", "")).strip() or "unknown",
        timestamp=datetime.now(timezone.utc).isoformat(), metadata=metadata,
    )
