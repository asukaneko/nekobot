from nbot.character.models import WorldBook, WorldBookEntry
from nbot.character.world_book_matcher import (
    WorldBookMatchResult,
    WorldBookRecallContext,
    WorldBookRecallConfig,
    _check_state_triggers,
    match_entries_v2,
)
from nbot.character.world_book_state import WorldBookStateStore


def _entry(**kwargs):
    params = {
        "id": "e1",
        "name": "条目1",
        "keywords": ["魔法"],
        "content": "魔法设定",
    }
    params.update(kwargs)
    return WorldBookEntry(**params)


def test_state_triggers_support_numeric_and_expressions():
    assert _check_state_triggers({"affection": 80}, _entry(state_triggers={"affection": ["80"]}))
    assert _check_state_triggers({"trust": 65}, _entry(state_triggers={"trust": [">=60"]}))
    assert not _check_state_triggers({"trust": 45}, _entry(state_triggers={"trust": [">=60"]}))
    assert _check_state_triggers(
        {"mood_intensity": 72}, _entry(state_triggers={"mood_intensity": ["60..100"]})
    )
    assert not _check_state_triggers(
        {"mood_intensity": 30}, _entry(state_triggers={"mood_intensity": ["60..100"]})
    )
    assert _check_state_triggers({"mood": "开心"}, _entry(state_triggers={"mood": ["!平静"]}))
    assert not _check_state_triggers({"mood": "平静"}, _entry(state_triggers={"mood": ["!平静"]}))
    assert _check_state_triggers({"energy": 50}, _entry(state_triggers={"energy": ["*"]}))
    assert _check_state_triggers(
        {"relationship": {"affection": 66}},
        _entry(state_triggers={"relationship.affection": [">=60"]}),
    )


def test_match_entries_respects_total_char_budget():
    long_entry = _entry(id="e-long", name="长条目", content="x" * 2000)
    second_entry = _entry(id="e-second", name="次条目", content="y" * 2000)
    books = [
        WorldBook(
            id="b1",
            name="书1",
            entries=[long_entry, second_entry],
            enabled=True,
        )
    ]
    context = WorldBookRecallContext(latest_user_message="魔法")
    config = WorldBookRecallConfig(max_total_chars=3000)

    results = match_entries_v2(context, books, character_id="", config=config)

    assert [r.entry.id for r in results] == ["e-long"]


def test_state_store_enforces_cooldown_and_max_injections(tmp_path):
    store = WorldBookStateStore(str(tmp_path / "wb_state.json"))
    scope = "char:conv"
    cooldown_entry = _entry(id="cd", cooldown_turns=2, max_injections_per_session=0)
    limited_entry = _entry(id="mx", cooldown_turns=0, max_injections_per_session=2)
    always_result = WorldBookMatchResult(
        entry=_entry(id="always", always_on=True),
        trigger_sources=["always"],
    )

    def results():
        return [
            WorldBookMatchResult(entry=cooldown_entry, trigger_sources=["user"]),
            WorldBookMatchResult(entry=limited_entry, trigger_sources=["user"]),
            always_result,
        ]

    turn1 = store.begin_turn(scope)
    assert [r.entry.id for r in store.filter_and_record(scope, results(), turn1)] == [
        "cd",
        "mx",
        "always",
    ]

    turn2 = store.begin_turn(scope)
    assert [r.entry.id for r in store.filter_and_record(scope, results(), turn2)] == [
        "mx",
        "always",
    ]

    turn3 = store.begin_turn(scope)
    assert [r.entry.id for r in store.filter_and_record(scope, results(), turn3)] == [
        "always",
    ]

    turn4 = store.begin_turn(scope)
    assert [r.entry.id for r in store.filter_and_record(scope, results(), turn4)] == [
        "cd",
        "always",
    ]


def test_state_store_survives_reload(tmp_path):
    path = str(tmp_path / "wb_state.json")
    store = WorldBookStateStore(path)
    scope = "char:conv"
    turn = store.begin_turn(scope)
    entry = _entry(id="cd", cooldown_turns=5)
    store.filter_and_record(scope, [WorldBookMatchResult(entry=entry, trigger_sources=["user"])], turn)

    reloaded = WorldBookStateStore(path)
    next_turn = reloaded.begin_turn(scope)
    kept = reloaded.filter_and_record(
        scope, [WorldBookMatchResult(entry=entry, trigger_sources=["user"])], next_turn
    )
    assert kept == []
