"""工具轨迹实时暂存（in-flight spool）测试。"""

import json
import os

import pytest

from nbot.core import session_store


@pytest.fixture()
def key(monkeypatch, tmp_path):
    monkeypatch.setattr(session_store, "_TOOL_TRACE_DIR", str(tmp_path / "traces"))
    monkeypatch.setattr(session_store, "_TOOL_TRACE_STEPS", {})
    return "test-session-1"


def _step(turn, name):
    return {
        "role": "tool",
        "tool_call_id": "call_1",
        "name": name,
        "content": json.dumps({"success": True}),
        "turn": turn,
        "iteration": 0,
    }


def test_trace_key_prefers_session_then_conversation():
    assert session_store.tool_trace_key("s1", "c1") == "s1"
    assert session_store.tool_trace_key("", "c1") == "c1"
    assert session_store.tool_trace_key("", "") == ""


def test_append_and_load_roundtrip(key):
    session_store.append_inflight_tool_step(key, _step("t1", "search_web"))
    session_store.append_inflight_tool_step(key, _step("t1", "read_memory"))

    steps = session_store.load_inflight_tool_steps(key)

    assert [s["name"] for s in steps] == ["search_web", "read_memory"]


def test_steps_persisted_to_disk_immediately(key):
    session_store.append_inflight_tool_step(key, _step("t1", "search_web"))

    path = session_store._tool_trace_path(key)
    assert os.path.exists(path)
    with open(path, encoding="utf-8") as handle:
        lines = [line for line in handle if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["name"] == "search_web"


def test_disk_fallback_when_memory_empty(key):
    session_store.append_inflight_tool_step(key, _step("t1", "search_web"))
    session_store._TOOL_TRACE_STEPS.pop(key, None)  # 模拟进程重启

    steps = session_store.load_inflight_tool_steps(key)

    assert [s["name"] for s in steps] == ["search_web"]


def test_load_filtered_by_turn(key):
    session_store.append_inflight_tool_step(key, _step("t1", "search_web"))
    session_store.append_inflight_tool_step(key, _step("t2", "read_memory"))

    assert [s["name"] for s in session_store.load_inflight_tool_steps(key, "t2")] == [
        "read_memory"
    ]


def test_clear_removes_memory_and_file(key):
    session_store.append_inflight_tool_step(key, _step("t1", "search_web"))
    path = session_store._tool_trace_path(key)

    session_store.clear_inflight_tool_steps(key)

    assert session_store.load_inflight_tool_steps(key) == []
    assert not os.path.exists(path)


def test_begin_resets_memory_but_keeps_disk(key):
    session_store.append_inflight_tool_step(key, _step("t1", "search_web"))
    session_store.begin_tool_trace(key)

    assert session_store._TOOL_TRACE_STEPS.get(key) is None
    # 磁盘上未收尾的轨迹保留，供恢复使用
    assert os.path.exists(session_store._tool_trace_path(key))


def test_bad_input_ignored(key):
    session_store.append_inflight_tool_step("", _step("t1", "x"))
    session_store.append_inflight_tool_step(key, None)  # type: ignore[arg-type]
    session_store.clear_inflight_tool_steps("")
    session_store.begin_tool_trace("")

    assert session_store.load_inflight_tool_steps("") == []
    assert session_store.load_inflight_tool_steps(key) == []


def test_step_cap(key):
    monkeypatch_cap = 5
    original = session_store._TOOL_TRACE_MAX_STEPS
    session_store._TOOL_TRACE_MAX_STEPS = monkeypatch_cap
    try:
        for index in range(monkeypatch_cap + 3):
            session_store.append_inflight_tool_step(
                key, _step("t1", f"tool_{index}")
            )
        steps = session_store._TOOL_TRACE_STEPS[key]
        assert len(steps) == monkeypatch_cap
        assert steps[-1]["name"] == f"tool_{monkeypatch_cap + 2}"
    finally:
        session_store._TOOL_TRACE_MAX_STEPS = original
