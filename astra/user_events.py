"""Stable user-visible event projection shared by interactive clients."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlparse

from .domain import ExecutionEvent
from .tool_gateway import BUSINESS_TOOL_DISPLAY_NAMES


TOOL_DISPLAY_NAMES: Mapping[str, str] = {
    **BUSINESS_TOOL_DISPLAY_NAMES,
    "request_user_input": "请求补充信息",
    "request_approval": "请求确认",
    "submit_task_result": "提交任务结果",
}

_HIDDEN_RUNTIME_TOOLS = frozenset(
    {"request_user_input", "request_approval", "submit_task_result"}
)

_INTERNAL_OUTPUT_LABELS = (
    "required_input_missing",
    "domain_result_evaluator_missing",
    "effect_request_hash",
    "PolicyDecision",
)

_HIDDEN_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "refresh_token",
        "oauth_token",
        "credential_pool",
        "credentials",
        "authorization",
        "token",
    }
)


_ANSI_RESET = "\033[0m"
_ANSI_ORANGE = "\033[38;5;209m"
_ANSI_BLUE = "\033[38;5;111m"
_ANSI_DIM = "\033[2m"


def _uses_color(stream: TextIO) -> bool:
    return bool(
        getattr(stream, "isatty", lambda: False)()
        and not os.environ.get("NO_COLOR")
        and os.environ.get("TERM", "").lower() != "dumb"
    )


def _styled(value: str, style: str, *, stream: TextIO) -> str:
    return f"{style}{value}{_ANSI_RESET}" if _uses_color(stream) else value


def visible_assistant_text(value: Any) -> str:
    """Extract ordered visible text and omit provider reasoning blocks."""

    if isinstance(value, str):
        visible = value
        for label in _INTERNAL_OUTPUT_LABELS:
            visible = re.sub(re.escape(label), "", visible, flags=re.IGNORECASE)
        return visible
    if isinstance(value, Mapping):
        kind = str(value.get("type") or "").lower()
        if kind in {"thinking", "reasoning", "analysis", "redacted_thinking"}:
            return ""
        if "content" in value:
            return visible_assistant_text(value["content"])
        text = value.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "".join(visible_assistant_text(item) for item in value)
    return ""


def _safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_value(child)
            for key, child in value.items()
            if str(key).lower() not in _HIDDEN_KEYS
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_safe_value(item) for item in value]
    return value


def _artifact_lines(
    artifact: Mapping[str, Any],
    *,
    prefix: str = "",
    depth: int = 0,
) -> list[str]:
    safe = _safe_value(artifact)
    lines: list[str] = []
    for key, value in safe.items():
        if value is None:
            continue
        label = str(key).replace("_", " ").strip().title()
        full_label = f"{prefix} {label}".strip()
        if isinstance(value, Mapping) and depth < 2:
            lines.extend(
                _artifact_lines(value, prefix=full_label, depth=depth + 1)
            )
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for index, item in enumerate(value, 1):
                if isinstance(item, Mapping) and depth < 2:
                    lines.extend(
                        _artifact_lines(
                            item,
                            prefix=f"{full_label} {index}",
                            depth=depth + 1,
                        )
                    )
        else:
            normalized_key = str(key).lower()
            if isinstance(value, str) and any(
                marker in normalized_key for marker in ("path", "file")
            ):
                existing = existing_artifact_path(value)
                if existing is None:
                    continue
                value = existing
            if isinstance(value, str) and "url" in normalized_key:
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    continue
            lines.append(f"{full_label}: {value}")
    return lines


@dataclass
class ConsoleEventRenderer:
    stream: TextIO
    details: bool = False
    _thinking: bool = field(default=False, init=False)
    # A delta sequence has exactly one following AssistantMessage in Hermes's
    # lifecycle.  Consume that message as the sequence terminator, then reset
    # so a later non-streaming message is still rendered normally.
    _streaming: bool = field(default=False, init=False)
    tool_calls: list[str] = field(default_factory=list, init=False)
    assistant_messages: int = field(default=0, init=False)

    def _finish_stream(self) -> None:
        """Terminate an inline delta sequence before a line-oriented event."""

        if self._streaming:
            print(file=self.stream, flush=True)
            self._streaming = False

    def render(self, event: ExecutionEvent) -> None:
        payload = event.payload
        if event.event_type == "ThinkingStarted":
            if not self._thinking:
                self._finish_stream()
                print(
                    _styled("· Thinking", _ANSI_DIM, stream=self.stream)
                    if _uses_color(self.stream)
                    else "· 正在思考…",
                    file=self.stream,
                    flush=True,
                )
                self._thinking = True
            return
        if event.event_type == "ThinkingStopped":
            self._thinking = False
            return
        if event.event_type == "AssistantMessageDelta":
            content = visible_assistant_text(payload.get("content"))
            if content:
                print(content, end="", file=self.stream, flush=True)
                self._streaming = True
            return
        if event.event_type == "AssistantMessage":
            content = visible_assistant_text(payload.get("content"))
            if content:
                self.assistant_messages += 1
                if self._streaming:
                    # This is the complete Hermes message for the deltas just
                    # rendered above.  It closes their lifecycle but is not
                    # rendered a second time.
                    self._finish_stream()
                else:
                    print(content, file=self.stream, flush=True)
            return
        if event.event_type == "ToolCallStarted":
            name = str(payload.get("tool_name") or "").strip()
            if name and name not in _HIDDEN_RUNTIME_TOOLS:
                self._finish_stream()
                self.tool_calls.append(name)
                display_name = TOOL_DISPLAY_NAMES.get(name, name)
                print(
                    (
                        _styled("● ", _ANSI_DIM, stream=self.stream)
                        + _styled(display_name, _ANSI_BLUE, stream=self.stream)
                    )
                    if _uses_color(self.stream)
                    else f"↳ {display_name}",
                    file=self.stream,
                    flush=True,
                )
            return
        if event.event_type == "BusinessArtifactProduced":
            artifact = payload.get("artifact")
            if not isinstance(artifact, Mapping):
                return
            lines = _artifact_lines(artifact)
            if lines:
                self._finish_stream()
                heading = (
                    _styled("Business result", _ANSI_ORANGE, stream=self.stream)
                    if _uses_color(self.stream)
                    else "业务信息"
                )
                print("\n" + heading + "\n", file=self.stream)
                print("\n".join(lines), file=self.stream, flush=True)


def existing_artifact_path(value: Any) -> str | None:
    if not isinstance(value, (str, Path)):
        return None
    path = Path(value).expanduser()
    return str(path.resolve()) if path.exists() else None
