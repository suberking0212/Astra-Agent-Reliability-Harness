"""Normalization and validation for user-supplied interaction answers."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Sequence


_ANSI_ESCAPE = re.compile(
    r"(?:\x1B\][^\x07\x1B]*(?:\x07|\x1B\\))"
    r"|(?:\x1B\[[0-?]*[ -/]*[@-~])"
    r"|(?:\x1B[@-_])"
)
_TOKEN_BOUNDARY_LEFT = r"(?<![A-Za-z0-9_.:@/+-])"
_TOKEN_BOUNDARY_RIGHT = r"(?![A-Za-z0-9_.:@/+-])"
_DEFAULT_SUBJECT_PATTERNS = {
    "customer": r"(?:cust|customer)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
    "order": r"order-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
    "ticket": r"ticket-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
    "complaint_ticket": r"ticket-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
    "work_order": r"ticket-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*",
}
_FIELD_SUBJECT_TYPES = {
    "customer_id": "customer",
    "order_id": "order",
    "ticket_id": "ticket",
    "complaint_ticket_id": "complaint_ticket",
    "work_order_id": "work_order",
}


class InvalidInteractionResponse(ValueError):
    """A candidate answer cannot authoritatively resolve its interaction."""

    def __init__(self, reason: str, user_message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.user_message = user_message


@dataclass(frozen=True)
class ValidatedInteractionResponse:
    normalized_response: str
    matched_identifiers: tuple[str, ...] = ()


class ApprovalCandidate(str, Enum):
    """Closed, business-independent interpretation of an approval answer."""

    APPROVED = "approved"
    DENIED = "denied"
    INVALID = "invalid"


_APPROVED_APPROVAL_ALIASES = frozenset({"y", "yes", "是", "确认", "同意"})
_DENIED_APPROVAL_ALIASES = frozenset({"n", "no", "否", "拒绝", "不同意"})


def normalize_user_input(value: str) -> str:
    """Remove terminal escapes and non-printing control/format characters."""

    without_ansi = _ANSI_ESCAPE.sub("", value)
    printable = "".join(
        character
        for character in without_ansi
        if not unicodedata.category(character).startswith("C")
    )
    return printable.strip()


def parse_approval_candidate(value: str) -> ApprovalCandidate:
    """Parse only the fixed exact-match approval protocol aliases."""

    normalized = normalize_user_input(value).casefold()
    if normalized in _APPROVED_APPROVAL_ALIASES:
        return ApprovalCandidate.APPROVED
    if normalized in _DENIED_APPROVAL_ALIASES:
        return ApprovalCandidate.DENIED
    return ApprovalCandidate.INVALID


def _string_sequence(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return tuple(str(item) for item in value if str(item))
    return ()


def _requirements(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    details = payload.get("details")
    merged = dict(details) if isinstance(details, Mapping) else {}
    for key in (
        "expected_fields",
        "expected_subject_types",
        "accepted_identifier_patterns",
        "resume_condition",
        "missing_fields",
    ):
        if key in payload:
            merged[key] = payload[key]
    return merged


def _accepted_patterns(requirements: Mapping[str, Any]) -> dict[str, str]:
    raw = requirements.get("accepted_identifier_patterns")
    if isinstance(raw, Mapping):
        return {
            str(key): str(value)
            for key, value in raw.items()
            if isinstance(value, str) and 0 < len(value) <= 300
        }
    values = _string_sequence(raw)
    return {f"custom:{index}": pattern for index, pattern in enumerate(values)}


def validate_interaction_response(
    value: str,
    payload: Mapping[str, Any],
) -> ValidatedInteractionResponse:
    """Normalize one candidate and enforce persisted structured requirements."""

    normalized = normalize_user_input(value)
    if not normalized:
        raise InvalidInteractionResponse(
            "empty_after_normalization",
            "该回答不包含可用内容，请重新输入；输入 /cancel 取消任务，或使用 /new <任务> 开始新任务。",
        )

    requirements = _requirements(payload)
    expected_fields = _string_sequence(
        requirements.get("expected_fields") or requirements.get("missing_fields")
    )
    expected_types = list(_string_sequence(requirements.get("expected_subject_types")))
    for field in expected_fields:
        subject_type = _FIELD_SUBJECT_TYPES.get(field.lower())
        if subject_type and subject_type not in expected_types:
            expected_types.append(subject_type)

    custom_patterns = _accepted_patterns(requirements)
    patterns: list[tuple[str, str]] = list(custom_patterns.items())
    for subject_type in expected_types:
        default_pattern = _DEFAULT_SUBJECT_PATTERNS.get(subject_type.lower())
        if default_pattern and subject_type not in custom_patterns:
            patterns.append((subject_type, default_pattern))

    identifier_required = bool(patterns or expected_types)
    if not identifier_required:
        return ValidatedInteractionResponse(normalized_response=normalized)

    matches: list[str] = []
    matched_types: set[str] = set()
    try:
        for subject_type, pattern in patterns:
            matcher = re.compile(
                _TOKEN_BOUNDARY_LEFT + "(?:" + pattern + ")" + _TOKEN_BOUNDARY_RIGHT,
                re.IGNORECASE,
            )
            found = [match.group(0) for match in matcher.finditer(normalized)]
            if found:
                matched_types.add(subject_type.lower())
                matches.extend(found)
    except re.error as error:
        raise InvalidInteractionResponse(
            "invalid_identifier_pattern",
            "当前补充信息要求无效，请取消任务后重新开始。",
        ) from error

    required_default_types = {
        subject_type.lower()
        for subject_type in expected_types
        if subject_type.lower() in _DEFAULT_SUBJECT_PATTERNS
    }
    if not matches or not required_default_types.issubset(matched_types):
        raise InvalidInteractionResponse(
            "required_identifier_missing",
            "该回答未提供请求的客户、订单或工单标识，请重新输入；输入 /cancel 取消任务，或使用 /new <任务> 开始新任务。",
        )
    return ValidatedInteractionResponse(
        normalized_response=normalized,
        matched_identifiers=tuple(dict.fromkeys(matches)),
    )
