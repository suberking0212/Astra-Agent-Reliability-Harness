from astra.execution_profiles import (
    ASTRA_CONTROLLED,
    ASTRA_CONTROLLED_DEBUG,
    ASTRA_FULL_HERMES,
    get_execution_profile,
    materialize_hermes_learning_config,
)


def test_stage1_profile_is_frozen_and_restricted():
    assert ASTRA_CONTROLLED.profile_id == "astra_controlled_debug"
    assert ASTRA_CONTROLLED is ASTRA_CONTROLLED_DEBUG
    assert ASTRA_CONTROLLED.astra_governed is True
    assert ASTRA_CONTROLLED.enable_skills is False
    assert ASTRA_CONTROLLED.enable_self_improvement is False
    assert ASTRA_CONTROLLED.enabled_toolsets == ("hermes-cli",)
    assert set(ASTRA_CONTROLLED.disabled_toolsets) >= {
        "file", "terminal", "skills", "memory", "code_execution"
    }
    assert "visible Astra tools" in ASTRA_CONTROLLED.launcher_guidance


def test_stage1_toolset_projection_does_not_publish_dynamic_plugin_names():
    assert ASTRA_CONTROLLED.hermes_toolsets(
        ("get_customer", "create_complaint_ticket")
    ) == ["hermes-cli"]


def test_full_profile_restores_native_hermes_surface_without_authority_changes():
    assert ASTRA_FULL_HERMES.profile_id == "astra_full_hermes"
    assert ASTRA_FULL_HERMES.astra_governed is True
    assert ASTRA_FULL_HERMES.disabled_toolsets == ()
    assert ASTRA_FULL_HERMES.enable_skills is True
    assert ASTRA_FULL_HERMES.enable_self_improvement is True
    assert ASTRA_FULL_HERMES.hermes_toolsets(()) == ["hermes-cli"]
    assert get_execution_profile("astra_controlled").profile_id == "astra_controlled_debug"


def test_learning_config_switches_debug_to_full_without_rebuilding_home():
    config = {"agent": {"skills": {"creation_nudge_interval": 99}, "memory": {"nudge_interval": 42}}}
    materialize_hermes_learning_config(config, ASTRA_CONTROLLED_DEBUG)
    assert config["agent"]["skills"]["creation_nudge_interval"] == 0
    assert config["agent"]["memory"]["nudge_interval"] == 0
    materialize_hermes_learning_config(config, ASTRA_FULL_HERMES)
    assert config["agent"]["skills"]["creation_nudge_interval"] == 15
    assert config["agent"]["memory"]["nudge_interval"] == 10


def test_clean_full_profile_materializes_learning_defaults():
    config = {}
    materialize_hermes_learning_config(config, ASTRA_FULL_HERMES)
    assert config["agent"]["skills"]["creation_nudge_interval"] == 15
    assert config["agent"]["memory"]["nudge_interval"] == 10
