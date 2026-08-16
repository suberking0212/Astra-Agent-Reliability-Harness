"""The Hermes plugin schema must remain a mechanics-free business protocol."""

from __future__ import annotations

from pathlib import Path

from astra.hermes_adapter import plugin
from astra.hermes_adapter.launch import bind_session_toolsets
from astra.capability_broker import CapabilityBroker
from astra.capabilities.customer_order import latest_customer_order_definition


ROOT = Path(__file__).resolve().parents[1]


class _Context:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.toolsets: dict[str, str] = {}

    def register_tool(self, *, name: str, schema: object, toolset: str, **_: object) -> None:
        self.tools[name] = schema
        self.toolsets[name] = toolset


def test_public_schema_exposes_only_semantic_broker_capability(monkeypatch):
    monkeypatch.setattr(plugin, "_transport_credential_cache", None)
    monkeypatch.setenv("ASTRA_RUNTIME_PLUGIN_AUTH", "schema-test-machine-auth")
    context = _Context()
    plugin.register(context)
    assert set(context.tools) == {"astra_get_latest_customer_order"}
    serialized = repr(context.tools).lower()
    for forbidden in ("execution_token", "execution_id", "attempt_id", "lease_token",
                      "lease_owner", "fencing", "runtime identity"):
        assert forbidden not in serialized
    broker = context.tools["astra_get_latest_customer_order"]
    assert set(broker["parameters"]["properties"]) == {"customer_id"}  # type: ignore[index]


def test_semantic_broker_metadata_describes_authoritative_governed_capability(monkeypatch):
    monkeypatch.setattr(plugin, "_transport_credential_cache", None)
    monkeypatch.setenv("ASTRA_RUNTIME_PLUGIN_AUTH", "schema-test-machine-auth")
    context = _Context()
    plugin.register(context)

    broker = context.tools["astra_get_latest_customer_order"]
    assert "latest authoritative order" in broker["description"]  # type: ignore[index]
    assert context.toolsets["astra_get_latest_customer_order"] == "astra_capabilities"

    manifest = (ROOT / ".hermes" / "plugins" / "astra-runtime" / "plugin.yaml").read_text(
        encoding="utf-8"
    )
    assert "  - astra_get_latest_customer_order" in manifest


def test_plugin_captures_transport_auth_then_removes_it_from_hermes_environment(monkeypatch):
    """A terminal child inherits env, so the plugin must not leave auth there."""
    monkeypatch.setattr(plugin, "_transport_credential_cache", None)
    monkeypatch.setenv("ASTRA_RUNTIME_PLUGIN_AUTH", "machine-only-secret")
    plugin._capture_transport_credential()
    assert "ASTRA_RUNTIME_PLUGIN_AUTH" not in __import__("os").environ
    assert plugin._transport_credential() == "machine-only-secret"


def test_launcher_keeps_transport_auth_until_the_new_hermes_process_registers_plugin():
    launcher = (ROOT / "scripts" / "start_astra_cli.sh").read_text(encoding="utf-8")
    assert "unset ASTRA_RUNTIME_PLUGIN_AUTH" not in launcher
    assert "plugin can capture and remove it during plugin discovery" in launcher


def test_preflight_does_not_create_a_second_hermes_session():
    preflight = (ROOT / "scripts" / "verify_astra_hermes_integration.py").read_text(
        encoding="utf-8"
    )
    assert "subprocess" not in preflight
    assert "chat" not in preflight
    assert "Parent-session binding" in preflight


def test_launcher_uses_parent_session_adapter_without_rewriting_toolset_config():
    launcher = (ROOT / "scripts" / "start_astra_cli.sh").read_text(encoding="utf-8")
    assert "-m astra.hermes_adapter.launch" in launcher
    assert 'platform_toolsets["cli"]' not in launcher
    assert "known_plugin_toolsets" not in launcher


def test_session_adapter_binds_astra_despite_historical_disabled_state():
    # Historical config is intentionally irrelevant to the invocation-level
    # binding: Hermes receives the dynamic plugin toolset on this parent
    # session even when it was previously known-but-absent in config.
    arguments = bind_session_toolsets([], "astra_full_hermes")
    assert arguments[:2] == ["--toolsets", "hermes-cli,astra_capabilities"]
    explicit = bind_session_toolsets(["chat", "--toolsets", "terminal,file"], "astra_full_hermes")
    assert explicit == ["--toolsets", "terminal,file,astra_capabilities", "chat"]


def test_business_skill_is_minimal_runtime_protocol_guidance():
    skill = (ROOT / ".hermes" / "astra-business-sandbox" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for required in (
        "Astra tools provide authoritative external capabilities.",
        "Use the matching\nAstra capability",
        "Do not manage Astra Tasks, leases, approvals, retries,\nor completion state",
        "handled behind the capability broker",
    ):
        assert required in skill
    for forbidden in ("查订单先", "投诉先", "退款先"):
        assert forbidden not in skill
    assert "authoritative answer" in skill


def test_capability_definition_projects_semantic_result_without_runtime_orchestration():
    class _Config:
        class _Sandbox:
            commerce_authority_domain = "commerce.test"
        business_sandbox_config = _Sandbox()
    _Config.config = _Config()

    class _Runtime:
        app = _Config()
        calls = []
        def submit(self, args):
            self.calls.append(("submit", args))
            return {"task_id": "internal-task"}
        def business(self, tool_name, args):
            self.calls.append(("business", tool_name, args))
            return {"ok": True, "result": {"data": {"orders": [
                {"order_id": "o-1", "status": "delivered", "delivered_at": "2026-01-02"},
            ]}, "receipt_id": "receipt-1"}}

    runtime = _Runtime()
    result = CapabilityBroker(runtime, (latest_customer_order_definition(authority_domain="commerce.test"),)).invoke(
        "astra_get_latest_customer_order", {"customer_id": "cust-1"})
    assert result["order_id"] == "o-1"
    assert result["receipt_ref"] == "receipt-1"
    assert result["evidence_ref"] == {"type": "execution_receipt", "id": "receipt-1"}
    assert [item[0] for item in runtime.calls] == ["submit", "business"]
    assert "task_id" not in result
