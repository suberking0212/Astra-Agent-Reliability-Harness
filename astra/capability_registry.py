"""Registry loader for Agent-facing capability definitions.

The core adapter only knows this generic loader. Product composition selects
which domain registry is installed.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable

from .capability_broker import CapabilityDefinition

# This is the Hermes-facing toolset identity.  It is intentionally distinct
# from the Runtime's capability registry reference below: Hermes discovers
# this toolset only after the project plugin has registered its tools.
HERMES_CAPABILITY_TOOLSET = "astra_capabilities"


def load_capabilities() -> tuple[CapabilityDefinition, ...]:
    reference = os.environ.get(
        "ASTRA_CAPABILITY_REGISTRY", "astra.hermes_capabilities:hermes_capabilities"
    )
    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise RuntimeError("invalid_astra_capability_registry")
    factory = getattr(importlib.import_module(module_name), attribute)
    if not callable(factory):
        raise RuntimeError("invalid_astra_capability_registry")
    definitions = tuple(factory())
    if not definitions:
        raise RuntimeError("astra_capability_registry_empty")
    return definitions
