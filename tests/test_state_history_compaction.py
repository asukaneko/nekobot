"""状态快照上限内合并（compaction）测试。"""

from types import SimpleNamespace

from nbot.character.state_history import (
    KEY_SOURCES,
    StateHistoryStore,
    compact_history,
)


def _snap(index, *, source="turn", affection=50.0, mood="平静", energy=80, trust=50.0):
    return {
        "ts": f"2026-01-01T00:00:{index:02d}",
        "source": source,
        "reason": "",
        "message_id": "",
        "mood": mood,
        "mood_intensity": 50,
        "energy": energy,
        "affection": affection,
        "trust": trust,
        "familiarity": 10.0,
        "dependency": 10.0,
        "security": 10.0,
        "jealousy": 0.0,
    }


def test_under_limit_is_untouched():
    history = [_snap(i) for i in range(5)]

    merged = compact_history(history, limit=10)

    assert merged == history


def test_keeps_head_and_tail():
    history = [_snap(i, affection=50 + i) for i in range(20)]

    merged = compact_history(history, limit=5)

    assert len(merged) == 5
    assert merged[0] == history[0]
    assert merged[-1] == history[-1]
    # 时间顺序不变
    assert [item["ts"] for item in merged] == sorted(item["ts"] for item in merged)


def test_key_source_snapshots_survive():
    history = [_snap(i, affection=50 + i * 0.01) for i in range(20)]
    history[7]["source"] = "manual"
    history[13]["source"] = "restore"

    merged = compact_history(history, limit=6)
    sources = {item["source"] for item in merged}

    assert "manual" in sources
    assert "restore" in sources
    assert len(merged) == 6


def test_large_delta_beats_tiny_changes():
    history = [_snap(i, affection=50 + i * 0.01) for i in range(20)]
    history[10]["affection"] = 95.0  # 关键跳变

    merged = compact_history(history, limit=4)

    assert history[10] in merged
    assert len(merged) == 4


def test_mood_change_counts_as_key_point():
    history = [_snap(i) for i in range(20)]
    for item in history[11:]:
        item["mood"] = "兴奋"

    merged = compact_history(history, limit=4)

    assert history[11] in merged


def test_compaction_is_idempotent():
    history = [_snap(i, affection=50 + i) for i in range(30)]

    once = compact_history(history, limit=8)
    twice = compact_history(once, limit=8)

    assert once == twice


def test_limit_two_keeps_only_ends():
    history = [_snap(i) for i in range(10)]

    merged = compact_history(history, limit=2)

    assert merged == [history[0], history[-1]]


def _state():
    return SimpleNamespace(mood="平静", mood_intensity=50, energy=80)


def _rel(affection, trust=50):
    return SimpleNamespace(
        affection=affection,
        trust=trust,
        familiarity=10,
        dependency=10,
        security=10,
        jealousy=0,
    )


def test_record_compacts_when_exceeding_limit(tmp_path):
    # 存储层会把上限夹到至少 10，这里按 10 验证
    store = StateHistoryStore(str(tmp_path), max_snapshots=10)

    for index in range(15):
        store.record("角色A", "user-1", _state(), _rel(50 + index))

    snapshots = store.list_snapshots("角色A", "user-1", limit=0)

    assert len(snapshots) == 10
    assert snapshots[-1]["affection"] == 64
    assert snapshots[0]["affection"] == 50


def test_record_keeps_manual_source_when_compacting(tmp_path):
    store = StateHistoryStore(str(tmp_path), max_snapshots=10)

    for index in range(3):
        store.record("角色A", "user-1", _state(), _rel(50 + index))
    store.record("角色A", "user-1", _state(), _rel(80), source="manual", force=True)
    for index in range(10):
        store.record("角色A", "user-1", _state(), _rel(80 + index * 0.1))

    snapshots = store.list_snapshots("角色A", "user-1", limit=0)

    assert len(snapshots) == 10
    assert any(item["source"] == "manual" for item in snapshots)
    assert "manual" in KEY_SOURCES


def test_compact_method_handles_legacy_oversized_data(tmp_path):
    store = StateHistoryStore(str(tmp_path), max_snapshots=10)
    legacy = [_snap(i, affection=50 + i) for i in range(25)]
    store._store.set("角色A::user-1", legacy)

    outcome = store.compact("角色A", "user-1")

    assert outcome == {"compact": True, "before": 25, "after": 10}
    assert len(store.list_snapshots("角色A", "user-1", limit=0)) == 10


def test_compact_method_noop_when_within_limit(tmp_path):
    store = StateHistoryStore(str(tmp_path), max_snapshots=10)
    store.record("角色A", "user-1", _state(), _rel(50))

    outcome = store.compact("角色A", "user-1")

    assert outcome == {"compact": False, "before": 1, "after": 1}


def test_diff_and_restore_range_unchanged(tmp_path):
    """压缩不改变快照字段，恢复范围仍是状态 + 六维关系。"""
    store = StateHistoryStore(str(tmp_path), max_snapshots=5)
    for index in range(8):
        store.record("角色A", "user-1", _state(), _rel(50 + index))

    snapshot = store.list_snapshots("角色A", "user-1", limit=1)[0]

    assert set(snapshot) >= {"mood", "mood_intensity", "energy", "affection", "trust"}
    assert "world_book" not in snapshot
