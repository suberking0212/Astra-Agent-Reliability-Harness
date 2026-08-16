"""Attach Astra's dynamic toolset before Hermes creates the parent CLI session."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from ..capability_registry import HERMES_CAPABILITY_TOOLSET
from ..execution_profiles import get_execution_profile


class SessionBindingError(RuntimeError):
    """The Hermes invocation cannot be bound to Astra safely."""


def _split_toolsets(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def bind_session_toolsets(arguments: Sequence[str], profile_id: str) -> list[str]:
    """Return Hermes arguments with Astra attached to this CLI invocation.

    Hermes 0.18.2's plugin API registers tools but has no hook that mutates an
    AIAgent's allowlist before ``agent.tools`` is snapshotted. Its documented
    per-invocation ``--toolsets`` option is therefore the narrow supported
    boundary for binding a dynamic plugin toolset to this one parent session.
    """
    args = list(arguments)
    explicit: list[str] | None = None
    stripped: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument in {"-t", "--toolsets"}:
            if index + 1 >= len(args):
                raise SessionBindingError(f"{argument} requires a value")
            explicit = _split_toolsets(args[index + 1])
            index += 2
            continue
        if argument.startswith("--toolsets="):
            explicit = _split_toolsets(argument.partition("=")[2])
            index += 1
            continue
        stripped.append(argument)
        index += 1

    profile = get_execution_profile(profile_id)
    selected = explicit if explicit is not None else profile.hermes_toolsets(())
    bound = list(dict.fromkeys([*selected, HERMES_CAPABILITY_TOOLSET]))
    if HERMES_CAPABILITY_TOOLSET not in bound:
        raise SessionBindingError("Astra capability toolset was not attached")
    return ["--toolsets", ",".join(bound), *stripped]


def _verify_registered_capability() -> None:
    """Fail before session creation if Hermes did not load Astra's toolset."""
    from hermes_cli.plugins import discover_plugins, get_plugin_manager, get_plugin_toolsets

    discover_plugins()
    plugin_toolsets = {key for key, _label, _description in get_plugin_toolsets()}
    if HERMES_CAPABILITY_TOOLSET not in plugin_toolsets:
        astra_plugins = [
            item for item in get_plugin_manager().list_plugins()
            if item.get("name") == "astra-runtime"
        ]
        detail = astra_plugins[0].get("error") if astra_plugins else "manifest not discovered"
        raise SessionBindingError(
            "Hermes plugin discovery did not register astra_capabilities: " + str(detail)
        )


def _verify_session_projection(arguments: Sequence[str]) -> None:
    """Use Hermes' schema provider to verify the soon-to-be parent allowlist."""
    try:
        toolsets = _split_toolsets(arguments[1])
    except (IndexError, TypeError) as exc:
        raise SessionBindingError("invalid bound Hermes toolset arguments") from exc
    from model_tools import get_tool_definitions

    definitions = get_tool_definitions(enabled_toolsets=toolsets, quiet_mode=True)
    names = {
        str(item.get("function", {}).get("name", ""))
        for item in definitions
        if isinstance(item, dict) and isinstance(item.get("function"), dict)
    }
    if "astra_get_latest_customer_order" not in names:
        raise SessionBindingError(
            "Hermes session tool projection omitted astra_get_latest_customer_order"
        )


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: python -m astra.hermes_adapter.launch HERMES [ARGS...]")
    # The executable argument documents and tests the launcher selection. This
    # process already runs under that executable's Python environment.
    _hermes_bin = sys.argv[1]
    import os
    profile_id = os.environ.get("ASTRA_EXECUTION_PROFILE", "astra_full_hermes")
    try:
        _verify_registered_capability()
        arguments = bind_session_toolsets(sys.argv[2:], profile_id)
        _verify_session_projection(arguments)
    except (SessionBindingError, ValueError) as exc:
        print(f"Astra/Hermes session binding failed: {exc}", file=sys.stderr)
        return 78
    # hermes_cli.main:main is Hermes' installed console-script entrypoint. Keep
    # plugin registry, captured transport credential, and the eventual parent
    # CLI session in this one process.
    from hermes_cli.main import main as hermes_main

    sys.argv = [_hermes_bin, *arguments]
    result = hermes_main()
    return int(result) if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
