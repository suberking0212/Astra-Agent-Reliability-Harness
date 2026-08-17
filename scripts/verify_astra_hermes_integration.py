#!/usr/bin/env python3
"""Fail-closed Runtime readiness check before the parent Hermes CLI starts.

Session tool binding is deliberately not probed here. The production launcher
binds the dynamic Astra toolset to the one parent Hermes invocation, and the
plugin audits that session's actual provider request.
"""

from __future__ import annotations

import json
import os
import sys
from typing import NoReturn
from urllib.request import Request, urlopen


def _fail(message: str) -> NoReturn:
    print(f"Astra/Hermes preflight failed: {message}", file=sys.stderr)
    raise SystemExit(78)


def main() -> int:
    endpoint = os.environ.get("ASTRA_RUNTIME_ENDPOINT", "").strip().rstrip("/")
    auth = os.environ.get("ASTRA_RUNTIME_PLUGIN_AUTH", "").strip()
    if not endpoint or not auth:
        _fail("Runtime endpoint or plugin authentication is missing")
    request = Request(
        endpoint + "/v1/plugin",
        data=json.dumps({"operation": "capabilities", "args": {}}).encode(),
        headers={"Content-Type": "application/json", "X-Astra-Plugin-Auth": auth},
        method="POST",
    )
    try:
        with urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode())
        result = json.loads(body["result"])
    except Exception as exc:
        _fail(f"Runtime authentication/readiness probe failed: {exc}")
    if not isinstance(result, dict) or result.get("ok") is not True:
        _fail("Runtime capabilities probe returned an invalid result")
    if "list_customer_orders" not in set(result.get("tools", ())):
        _fail("Runtime capability catalog is incomplete")
    print(
        "Astra/Hermes preflight: Runtime ready and capability catalog authenticated. "
        "Parent-session binding will be verified on its real provider request.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
