"""酒馆角色卡互转的纯函数测试"""

import base64
import json

import pytest

from nbot.character import tavern_card


def _native_card() -> dict:
    return {
        "id": "card-1",
        "name": "小春",
        "description": "短发女孩",
        "basicInfo": "身高 160cm",
        "personality": "温柔",
        "scenario": "春日的教室",
        "firstMessage": "（抬头）你来啦。",
        "exampleDialogues": "<user>你好\n<assistant>你好呀",
        "responseFormat": "（动作）语言",
        "rules": ["不要出戏", "保持温柔"],
        "tags": ["日常", "治愈"],
        "state": {"affection": 62, "mood": "开心"},
        "alternateGreetings": ["「……你迟到了。」", "（趴在桌上）唔……"],
        "tavern": {
            "creator": "tester",
            "creatorNotes": "测试卡",
            "characterVersion": "1.2",
            "extensions": {"foo": "bar"},
        },
    }


def test_detect_card_format_recognizes_variants():
    assert tavern_card.detect_card_format({"spec": "chara_card_v3", "data": {}}) == "tavern_v3"
    assert tavern_card.detect_card_format({"spec": "chara_card_v2", "data": {}}) == "tavern_v2"
    assert tavern_card.detect_card_format({"name": "a", "first_mes": "hi"}) == "tavern_v1"
    assert tavern_card.detect_card_format({"name": "a", "firstMessage": "hi"}) == "native"


def test_normalize_tavern_v2_maps_fields():
    raw = {
        "spec": "chara_card_v2",
        "spec_version": "2.0",
        "data": {
            "name": "阿岚",
            "description": "剑士",
            "personality": "冷静",
            "scenario": "边境小镇",
            "first_mes": "拔剑吧。",
            "mes_example": "<user>hi",
            "system_prompt": "你是阿岚",
            "alternate_greetings": ["……你来了。", "别挡路。"],
            "tags": ["奇幻"],
            "creator_notes": "作者备注",
            "character_book": {
                "name": "小镇设定",
                "entries": [
                    {
                        "keys": ["酒馆", "酒"],
                        "content": "镇上只有一间酒馆。",
                        "name": "酒馆",
                        "insertion_order": 120,
                        "enabled": True,
                    },
                    {
                        "keys": ["城堡"],
                        "secondary_keys": ["王座"],
                        "content": "城堡在山上。",
                        "selective": True,
                        "constant": False,
                        "position": "after_char",
                    },
                ],
            },
        },
    }

    native, meta = tavern_card.normalize_tavern_card(raw)

    assert native["name"] == "阿岚"
    assert native["firstMessage"] == "拔剑吧。"
    assert native["exampleDialogues"] == "<user>hi"
    assert native["systemPrompt"] == "你是阿岚"
    assert native["alternateGreetings"] == ["……你来了。", "别挡路。"]
    assert native["tags"] == ["奇幻"]
    assert native["state"]["affection"] == 50
    assert meta["format"] == "tavern_v2"
    assert meta["tavern"]["creatorNotes"] == "作者备注"

    book = native["characterBook"]
    assert book["name"] == "小镇设定"
    assert len(book["entries"]) == 2

    first, second = book["entries"]
    assert first["keywords"] == ["酒馆", "酒"]
    assert first["priority"] == 120
    assert first["always_on"] is False
    assert first["position"] == ""
    assert second["keywords"] == ["城堡", "王座"]
    assert second["match_mode"] == "all"
    assert second["position"] == "after_char"


def test_normalize_requires_name():
    with pytest.raises(tavern_card.TavernCardError):
        tavern_card.normalize_tavern_card({"description": "无名"})


def test_normalize_falls_back_to_first_alternate_greeting():
    native, meta = tavern_card.normalize_tavern_card(
        {"name": "a", "alternate_greetings": ["第一条", "第二条"]}
    )
    assert native["firstMessage"] == "第一条"
    assert any("备用开场白" in item for item in meta["warnings"])


def test_round_trip_native_to_v3_back():
    native = _native_card()
    card = tavern_card.to_tavern_card(native, "v3")
    assert card["spec"] == "chara_card_v3"
    assert card["data"]["alternate_greetings"] == native["alternateGreetings"]
    assert card["data"]["extensions"]["nekobot"]["basicInfo"] == "身高 160cm"
    assert card["data"]["extensions"]["nekobot"]["rules"] == native["rules"]
    assert card["data"]["creator"] == "tester"

    restored, meta = tavern_card.normalize_tavern_card(card)
    assert restored["name"] == native["name"]
    assert restored["basicInfo"] == native["basicInfo"]
    assert restored["rules"] == native["rules"]
    assert restored["responseFormat"] == native["responseFormat"]
    assert restored["firstMessage"] == native["firstMessage"]
    assert restored["alternateGreetings"] == native["alternateGreetings"]
    assert restored["state"]["affection"] == 62
    assert restored["tavern"]["creatorNotes"] == "测试卡"
    assert meta["tavern"]["characterVersion"] == "1.2"


def test_round_trip_character_book():
    native = _native_card()
    native["characterBook"] = {
        "name": "设定集",
        "description": "世界观",
        "entries": [
            {
                "name": "魔法",
                "keywords": ["魔法", "咒语"],
                "content": "魔法需要咏唱。",
                "priority": 40,
                "position": "at_depth",
                "depth": 2,
                "always_on": True,
                "match_mode": "all",
            }
        ],
    }

    card = tavern_card.to_tavern_card(native, "v2")
    entry = card["data"]["character_book"]["entries"][0]
    assert entry["keys"] == ["魔法", "咒语"]
    assert entry["insertion_order"] == 40
    assert entry["constant"] is True
    assert entry["selective"] is True
    assert entry["extensions"]["position"] == "at_depth"
    assert entry["extensions"]["nekobot_position"] == "at_depth"

    restored, _ = tavern_card.normalize_tavern_card(card)
    back = restored["characterBook"]["entries"][0]
    assert back["keywords"] == ["魔法", "咒语"]
    assert back["priority"] == 40
    assert back["position"] == "at_depth"
    assert back["depth"] == 2
    assert back["always_on"] is True


def test_png_text_round_trip_replaces_existing_chunk():
    image = tavern_card.build_placeholder_png(6, 4)
    first = tavern_card.build_card_png({"name": "旧"}, image)
    second = tavern_card.build_card_png({"name": "新"}, first)

    payload = tavern_card.extract_card_payload(second)
    assert payload["data"]["name"] == "新"

    texts = tavern_card.read_png_text_chunks(second)
    assert "ccv3" in texts and "chara" in texts
    # 每个关键词只保留一份
    assert second.count(b"ccv3\x00") == 1


def test_extract_payload_supports_plain_json_text_chunk():
    image = tavern_card.build_placeholder_png(2, 2)
    card = {"spec": "chara_card_v2", "data": {"name": "纯文本", "first_mes": "hi"}}
    raw = tavern_card.insert_png_text_chunks(image, {"chara": json.dumps(card, ensure_ascii=False)})

    payload = tavern_card.extract_card_payload(raw)
    assert payload["data"]["name"] == "纯文本"

    native, meta = tavern_card.parse_card_bytes("card.png", raw)
    assert native["name"] == "纯文本"
    assert meta["format"] == "png_tavern_v2"


def test_extract_payload_supports_base64_with_whitespace():
    image = tavern_card.build_placeholder_png(2, 2)
    card = {"spec": "chara_card_v2", "data": {"name": "换行", "first_mes": "hi"}}
    encoded = base64.b64encode(json.dumps(card).encode("utf-8")).decode("ascii")
    chunked = "\n".join(encoded[i:i + 16] for i in range(0, len(encoded), 16))
    raw = tavern_card.insert_png_text_chunks(image, {"chara": chunked})

    assert tavern_card.extract_card_payload(raw)["data"]["name"] == "换行"


def test_parse_card_bytes_errors():
    with pytest.raises(tavern_card.TavernCardError):
        tavern_card.parse_card_bytes("card.txt", b"hello")
    with pytest.raises(tavern_card.TavernCardError):
        tavern_card.parse_card_bytes("card.png", tavern_card.build_placeholder_png())
    with pytest.raises(tavern_card.TavernCardError):
        tavern_card.parse_card_bytes("card.json", b"{not json}")


def test_greeting_extraction_and_pick():
    native = _native_card()
    greetings = tavern_card.extract_greetings(native)
    assert greetings == [
        "（抬头）你来啦。",
        "「……你迟到了。」",
        "（趴在桌上）唔……",
    ]

    text, index = tavern_card.pick_greeting(native, index=2)
    assert (text, index) == ("（趴在桌上）唔……", 2)

    text, index = tavern_card.pick_greeting(native, randomize=True)
    assert text in greetings and 0 <= index < len(greetings)

    assert tavern_card.pick_greeting({}) == ("", -1)
    assert tavern_card.pick_greeting({"firstMessage": "仅一条"}, index=9) == ("仅一条", 0)


def test_build_card_png_uses_placeholder_when_base_not_png():
    png = tavern_card.build_card_png({"name": "无立绘"}, b"not a png")
    assert tavern_card.is_png(png)
    assert tavern_card.extract_card_payload(png)["data"]["name"] == "无立绘"
