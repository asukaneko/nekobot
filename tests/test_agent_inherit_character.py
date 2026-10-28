"""P2-8：agent 会话继承角色能力的开关测试（ModePolicy / 会话归一化）。"""

import pytest

from nbot.core.ai_pipeline import ModePolicy, parse_bool_flag
from nbot.services import chat_service


def test_agent_default_keeps_current_behavior():
    policy = ModePolicy.from_metadata({"session_mode": "agent"})

    assert policy.is_agent is True
    assert policy.inherit_character is False
    assert policy.inherits_character is False
    assert policy.inject_character_memories_legacy is False
    assert policy.inject_memory_fs is False
    assert policy.inject_image_capability is False
    assert policy.inject_real_time_prompt_fallback is False
    assert policy.run_auto_memory is False
    assert policy.try_send_image is False


def test_agent_with_switch_enables_character_features():
    policy = ModePolicy.from_metadata(
        {"session_mode": "agent", "agent_inherit_character": True}
    )

    assert policy.is_agent is True  # 仍然是 agent 会话（保留工具能力）
    assert policy.inherit_character is True
    assert policy.inherits_character is True
    assert policy.inject_character_memories_legacy is True
    assert policy.inject_memory_fs is True
    assert policy.inject_image_capability is True
    assert policy.inject_real_time_prompt_fallback is True
    assert policy.run_auto_memory is True
    # 自动生图标签仍然关闭：agent 会话用 generate_image 工具显式出图
    assert policy.try_send_image is False


def test_character_mode_unaffected_by_switch():
    for metadata in (
        {"session_mode": "character"},
        {"session_mode": "character", "agent_inherit_character": True},
        {},
    ):
        policy = ModePolicy.from_metadata(metadata)
        assert policy.inherits_character is True
        assert policy.try_send_image is True


@pytest.mark.parametrize(
    "value",
    [True, 1, "1", "true", "TRUE", "yes", "on", "y", "t"],
)
def test_flag_truthy_values(value):
    assert parse_bool_flag(value) is True
    assert ModePolicy.from_metadata(
        {"session_mode": "agent", "agent_inherit_character": value}
    ).inherits_character is True


@pytest.mark.parametrize("value", [False, 0, "", "0", "false", "off", "no", None])
def test_flag_falsy_values(value):
    assert parse_bool_flag(value) is False
    assert ModePolicy.from_metadata(
        {"session_mode": "agent", "agent_inherit_character": value}
    ).inherits_character is False


def test_character_features_enabled_by_session_dict():
    assert ModePolicy.character_features_enabled({"session_mode": "character"}) is True
    assert ModePolicy.character_features_enabled({}) is True
    assert ModePolicy.character_features_enabled({"session_mode": "agent"}) is False
    assert (
        ModePolicy.character_features_enabled(
            {"session_mode": "agent", "agent_inherit_character": True}
        )
        is True
    )
    assert (
        ModePolicy.character_features_enabled(
            {"session_mode": "agent", "agent_inherit_character": "false"}
        )
        is False
    )
    assert ModePolicy.character_features_enabled(None) is False


def _messages():
    return [
        {"role": "system", "content": "角色卡设定", "timestamp": "t0"},
        {"role": "user", "content": "你好", "timestamp": "t1"},
        {"role": "assistant", "content": "在的", "timestamp": "t2"},
        {"role": "tool", "content": "ignored", "timestamp": "t3"},
    ]


def test_agent_normalization_blanks_system_prompt_without_switch():
    normalized = chat_service._normalize_canonical_qq_messages(
        _messages(), session_mode="agent"
    )

    assert [m["role"] for m in normalized] == ["system", "user", "assistant"]
    assert normalized[0]["content"] == ""


def test_agent_normalization_keeps_system_prompt_with_switch():
    normalized = chat_service._normalize_canonical_qq_messages(
        _messages(), session_mode="agent", inherit_character=True
    )

    assert normalized[0]["content"] == "角色卡设定"


def test_character_normalization_keeps_system_prompt():
    normalized = chat_service._normalize_canonical_qq_messages(
        _messages(), session_mode="character"
    )

    assert normalized[0]["content"] == "角色卡设定"
