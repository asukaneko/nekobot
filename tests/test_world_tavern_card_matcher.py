"""酒馆卡增强字段（probability / use_regex / match_whole_words）与世界书匹配联动。"""

from nbot.character.models import WorldBook, WorldBookEntry
from nbot.character.tavern_card import (
    character_book_entry_to_world_book_entry,
    normalize_tavern_card,
    world_book_entry_to_tavern_entry,
)
from nbot.character.world_book_matcher import (
    WorldBookRecallConfig,
    WorldBookRecallContext,
    debug_match_entries,
    match_entries_v2,
)


class _FixedRng:
    """固定返回值的随机源，用于确定性地验证概率判定。"""

    def __init__(self, value: float):
        self.value = value

    def random(self) -> float:
        return self.value


def _entry(**kwargs):
    return WorldBookEntry(**kwargs)


def _books(*entries):
    return [WorldBook(id="b1", name="书1", entries=list(entries), enabled=True)]


def _matched_ids(results):
    return [r.entry.id for r in results]


# --------------------------- 酒馆卡字段解析 --------------------------- #


def test_tavern_entry_imports_extended_fields():
    entry = character_book_entry_to_world_book_entry(
        {
            "keys": ["魔法"],
            "content": "魔法设定",
            "extensions": {"probability": 40, "use_regex": True, "match_whole_words": True},
        }
    )
    assert entry["probability"] == 40
    assert entry["use_regex"] is True
    assert entry["match_whole_words"] is True


def test_tavern_entry_imports_fields_from_entry_root():
    entry = character_book_entry_to_world_book_entry(
        {"keys": ["魔法"], "content": "设定", "use_regex": True, "match_whole_words": False}
    )
    assert entry["use_regex"] is True
    assert entry["probability"] == 100


def test_tavern_probability_is_clamped():
    too_high = character_book_entry_to_world_book_entry(
        {"keys": ["a"], "content": "x", "extensions": {"probability": 150}}
    )
    too_low = character_book_entry_to_world_book_entry(
        {"keys": ["a"], "content": "x", "extensions": {"probability": -5}}
    )
    assert too_high["probability"] == 100
    assert too_low["probability"] == 0


def test_tavern_export_round_trips_extended_fields():
    tavern_entry = world_book_entry_to_tavern_entry(
        {
            "keywords": ["魔.*"],
            "content": "x",
            "probability": 55,
            "use_regex": True,
            "match_whole_words": True,
        }
    )
    assert tavern_entry["extensions"]["probability"] == 55
    assert tavern_entry["use_regex"] is True
    assert tavern_entry["match_whole_words"] is True

    back = character_book_entry_to_world_book_entry(tavern_entry)
    assert back["probability"] == 55
    assert back["use_regex"] is True
    assert back["match_whole_words"] is True


def test_normalize_tavern_card_warns_on_low_probability_and_bad_regex():
    native, meta = normalize_tavern_card(
        {
            "spec": "chara_card_v2",
            "data": {
                "name": "露娜",
                "character_book": {
                    "entries": [
                        {
                            "keys": ["魔(法"],
                            "content": "坏正则",
                            "use_regex": True,
                            "extensions": {"probability": 30},
                        }
                    ]
                },
            },
        }
    )
    warnings = meta["warnings"]
    assert any("30%" in item for item in warnings)
    assert any("无法编译" in item for item in warnings)
    assert native["characterBook"]["entries"][0]["probability"] == 30


# --------------------------- 正则 / 整词匹配 --------------------------- #


def test_regex_keyword_triggers_on_pattern():
    entry = _entry(id="e1", keywords=[r"魔[法导]"], content="设定", use_regex=True)
    results = match_entries_v2(
        WorldBookRecallContext(latest_user_message="他念起了魔导书"),
        _books(entry),
        character_id="",
        config=WorldBookRecallConfig(),
    )
    assert _matched_ids(results) == ["e1"]
    assert results[0].matched_keywords == [r"魔[法导]"]


def test_regex_keyword_invalid_pattern_does_not_crash():
    entry = _entry(id="e1", keywords=["魔(法"], content="设定", use_regex=True)
    results = match_entries_v2(
        WorldBookRecallContext(latest_user_message="魔法"),
        _books(entry),
        character_id="",
        config=WorldBookRecallConfig(),
    )
    assert results == []


def test_match_whole_words_rejects_substrings():
    hit = _entry(id="e1", keywords=["mage"], content="设定", match_whole_words=True)
    miss = _entry(id="e2", keywords=["mage"], content="设定", match_whole_words=True)

    assert _matched_ids(
        match_entries_v2(
            WorldBookRecallContext(latest_user_message="a mage appears"),
            _books(hit),
            character_id="",
            config=WorldBookRecallConfig(),
        )
    ) == ["e1"]
    assert (
        match_entries_v2(
            WorldBookRecallContext(latest_user_message="mages appear"),
            _books(miss),
            character_id="",
            config=WorldBookRecallConfig(),
        )
        == []
    )


def test_match_whole_words_falls_back_to_substring_for_cjk():
    entry = _entry(id="e1", keywords=["魔法"], content="设定", match_whole_words=True)
    results = match_entries_v2(
        WorldBookRecallContext(latest_user_message="古老的魔法阵"),
        _books(entry),
        character_id="",
        config=WorldBookRecallConfig(),
    )
    assert _matched_ids(results) == ["e1"]


def test_plain_substring_still_matches_without_flags():
    entry = _entry(id="e1", keywords=["mage"], content="设定")
    results = match_entries_v2(
        WorldBookRecallContext(latest_user_message="mages appear"),
        _books(entry),
        character_id="",
        config=WorldBookRecallConfig(),
    )
    assert _matched_ids(results) == ["e1"]


# --------------------------- 概率触发 --------------------------- #


def test_probability_gates_triggered_entry():
    entry = _entry(id="e1", keywords=["魔法"], content="设定", probability=30)
    context = WorldBookRecallContext(latest_user_message="魔法")
    books = _books(entry)
    config = WorldBookRecallConfig()

    passed = match_entries_v2(context, books, character_id="", config=config, rng=_FixedRng(0.1))
    skipped = match_entries_v2(context, books, character_id="", config=config, rng=_FixedRng(0.9))
    assert _matched_ids(passed) == ["e1"]
    assert skipped == []


def test_probability_extremes_and_always_on():
    never = _entry(id="never", keywords=["魔法"], content="设定", probability=0)
    always = _entry(id="always", keywords=["魔法"], content="设定", probability=100)
    constant = _entry(id="constant", content="常驻", always_on=True, probability=0)
    context = WorldBookRecallContext(latest_user_message="魔法")
    results = match_entries_v2(
        context,
        _books(never, always, constant),
        character_id="",
        config=WorldBookRecallConfig(),
        rng=_FixedRng(0.99),
    )
    assert _matched_ids(results) == ["always", "constant"] or _matched_ids(results) == [
        "constant",
        "always",
    ]


def test_debug_match_reports_probability_skip():
    entry = _entry(id="e1", keywords=["魔法"], content="设定", probability=20)
    diagnostics = debug_match_entries(
        WorldBookRecallContext(latest_user_message="魔法"),
        _books(entry),
        character_id="",
        config=WorldBookRecallConfig(),
        rng=_FixedRng(0.8),
    )
    item = diagnostics[0]
    assert item["matched"] is False
    assert item["probability"] == 20
    assert "触发概率" in item["reason"]


def test_debug_match_marks_regex_keyword_hit():
    entry = _entry(id="e1", keywords=[r"魔[法导]"], content="设定", use_regex=True)
    diagnostics = debug_match_entries(
        WorldBookRecallContext(latest_user_message="魔导书"),
        _books(entry),
        character_id="",
        config=WorldBookRecallConfig(),
    )
    item = diagnostics[0]
    assert item["matched"] is True
    assert item["use_regex"] is True
    assert item["missed_keywords"] == []


# --------------------------- 持久化往返 --------------------------- #


def test_world_book_store_round_trips_extended_fields(tmp_path):
    from nbot.character.storage.world_book_store import WorldBookStore

    store = WorldBookStore(str(tmp_path))
    book = store.create(name="往返书")
    created = store.add_entry(
        book.id,
        {
            "name": "正则条目",
            "keywords": [r"魔[法导]"],
            "content": "设定",
            "probability": 40,
            "use_regex": True,
            "match_whole_words": True,
        },
    )
    assert created is not None

    reloaded = WorldBookStore(str(tmp_path)).get_entry(book.id, created.id)
    assert reloaded is not None
    assert reloaded.probability == 40
    assert reloaded.use_regex is True
    assert reloaded.match_whole_words is True

    updated = store.update_entry(book.id, created.id, probability=55, use_regex=False)
    assert updated is not None and updated.probability == 55
    assert WorldBookStore(str(tmp_path)).get_entry(book.id, created.id).use_regex is False


def test_legacy_entry_without_new_fields_reads_with_defaults():
    entry = WorldBookEntry.from_dict({"id": "old", "name": "旧条目", "keywords": ["魔法"]})
    assert entry.probability == 100
    assert entry.use_regex is False
    assert entry.match_whole_words is False

    from_dict = WorldBookEntry.from_dict(
        {"id": "bad", "probability": "abc", "use_regex": 1, "match_whole_words": None}
    )
    assert from_dict.probability == 100
    assert from_dict.use_regex is True
    assert from_dict.match_whole_words is False
