"""角色导入链路接世界模拟角色条目（resolver.from_native_card + personality 钩子）。"""

from nbot.web.routes.personality import _attach_world_character, _finalize_imported_card
from nbot.world.character_resolver import CharacterResolver

_NATIVE_CARD = {
    "id": "preset_luna",
    "name": "露娜",
    "description": "钟塔的守夜人。",
    "basicInfo": "女性 / 27 岁 / 守夜人",
    "personality": "冷静、寡言",
    "scenario": "雾海边境的钟塔",
    "firstMessage": "你来得太早了。",
    "responseFormat": "简短，少用形容词",
    "rules": ["不主动透露钟声的秘密"],
    "tags": ["守夜人", "奇幻"],
    "state": {"affection": 30, "trust": 40},
    "characterBookId": "book_1",
}


def test_from_native_card_builds_world_character():
    resolved = CharacterResolver().from_native_card(_NATIVE_CARD)

    assert resolved["name"] == "露娜"
    assert resolved["source_type"] == "preset"
    assert resolved["source_id"] == "preset_luna"

    card = resolved["card"]
    assert card["personality"] == "冷静、寡言"
    assert card["scenario"] == "雾海边境的钟塔"
    assert card["first_message"] == "你来得太早了。"
    assert card["speech_style"] == "简短，少用形容词"
    assert card["rules"] == ["不主动透露钟声的秘密"]
    assert card["basic_info"] == "女性 / 27 岁 / 守夜人"
    assert card["tags"] == ["守夜人", "奇幻"]
    assert card["state"]["affection"] == 30
    assert card["character_book_id"] == "book_1"


def test_from_native_card_rejects_card_without_name():
    try:
        CharacterResolver().from_native_card({"description": "没有名字"})
    except ValueError as exc:
        assert "名称" in str(exc)
    else:  # pragma: no cover - 走到这里说明没抛错
        raise AssertionError("缺少名称时应当抛 ValueError")


def test_attach_world_character_is_idempotent_and_safe():
    card = dict(_NATIVE_CARD)
    first = _attach_world_character(card)
    second = _attach_world_character(first)

    assert second["worldCharacter"]["name"] == "露娜"
    assert second["worldCharacter"]["source_id"] == "preset_luna"

    # 缺名称时不抛错，也不写脏字段
    broken = {"description": "没有名字"}
    assert "worldCharacter" not in _attach_world_character(broken)


def test_finalize_imported_card_attaches_world_character():
    character = _finalize_imported_card(dict(_NATIVE_CARD), {"format": "native", "warnings": []})

    assert character["systemPrompt"]
    assert character["worldCharacter"]["card"]["personality"] == "冷静、寡言"
    assert character["worldCharacter"]["source_type"] == "preset"


def test_tavern_card_path_still_works_for_resolver():
    payload = {
        "spec": "chara_card_v2",
        "data": {
            "name": "伊琳",
            "description": "档案员",
            "first_mes": "档案不在这里。",
            "tags": ["档案"],
        },
    }
    resolved = CharacterResolver().from_tavern_card(payload, source_id="tavern_1")

    assert resolved["name"] == "伊琳"
    assert resolved["source_type"] == "tavern"
    assert resolved["source_id"] == "tavern_1"
    assert resolved["card"]["tavern_format"] == "tavern_v2"
