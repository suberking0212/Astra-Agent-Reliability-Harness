"""Deterministic JSON and hashing primitives for frozen Phase 3 contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel


def _json_ready(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(child) for child in value]
    if isinstance(value, Enum):
        return _json_ready(value.value)
    if isinstance(value, datetime):
        rendered = value.isoformat()
        return rendered[:-6] + "Z" if rendered.endswith("+00:00") else rendered
    if isinstance(value, date):
        return value.isoformat()
    return value


def canonical_json(value: Any) -> str:
    """Serialize JSON data with the Phase 3 v1 canonicalization profile.

    The frozen contracts require UTF-8, sorted object keys, stable array order,
    no insignificant whitespace, and rejection of NaN/Infinity.  Phase 3 data
    models deliberately avoid floating-point business amounts; versioned
    normalizers must convert those values to stable representations first.
    """

    return json.dumps(
        _json_ready(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_digest(value: Any) -> str:
    """Return the normative ``sha256:<hex>`` digest for JSON data."""

    payload = canonical_json(value).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def without_key(value: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Copy a mapping while excluding one envelope field."""

    return {name: child for name, child in value.items() if name != key}
