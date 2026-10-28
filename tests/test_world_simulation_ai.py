"""WorldSimulation 的 AI 分支测试（全部注入 fake model_client，不依赖真模型）。"""

import json
import tempfile

from nbot.world import WorldSimulation, WorldStore


class _FakeClient:
    """记录调用次数并返回预设结果的假模型客户端。"""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, messages):
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _world(tmpdir, **overrides):
    store = WorldStore(tmpdir)
    payload = {"name": "雾海边境", "tension": 10}
    payload.update(overrides)
    bundle = store.create_world(payload)
    return store, bundle["world"]["world_id"], bundle["current_node"]["node_id"]


def test_ai_branch_marks_ai_used_and_applies_state_delta():
    with tempfile.TemporaryDirectory() as tmpdir:
        store, world_id, node_id = _world(tmpdir)
        client = _FakeClient(
            {
                "display": [{"type": "narration", "content": "钟声从雾里传来。"}],
                "state_delta": {"global_state": {"tension": "+7"}},
                "new_events": [{"type": "world_event", "title": "钟声", "content": "一声钟鸣。"}],
                "suggested_actions": ["走向钟塔"],
                "branch_hint": "钟塔前适合分支。",
            }
        )

        result = WorldSimulation(store, model_client=client).step(world_id, node_id, "我抬头听。")

        assert result.ai_used is True
        assert len(client.calls) == 1
        assert result.state["global_state"]["tension"] == 17
        assert result.display[0]["type"] == "narration"
        # player_action + narration + world_event
        assert len(result.events) == 3
        assert result.suggested_actions == ["走向钟塔"]
        assert result.branch_hint == "钟塔前适合分支。"


def test_ai_branch_parses_fenced_json_output():
    with tempfile.TemporaryDirectory() as tmpdir:
        store, world_id, node_id = _world(tmpdir)
        body = {"display": [{"type": "dialogue", "content": "你来了。"}], "state_delta": {}}
        client = _FakeClient("好的：\n```json\n" + json.dumps(body, ensure_ascii=False) + "\n```")

        result = WorldSimulation(store, model_client=client).step(world_id, node_id, "我推门。")

        assert result.ai_used is True
        assert result.display[0]["content"] == "你来了。"


def test_invalid_json_degrades_to_rule_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        store, world_id, node_id = _world(tmpdir)
        client = _FakeClient("这不是 JSON，只是一段散文。")

        result = WorldSimulation(store, model_client=client).step(world_id, node_id, "我环顾四周。")

        assert result.ai_used is False
        assert len(client.calls) == 1
        assert result.display[-1]["type"] == "system_note"
        assert len(result.events) == 2
        assert result.events[0]["type"] == "player_action"
        assert result.suggested_actions == []


def test_zero_effect_payload_degrades_to_rule_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        store, world_id, node_id = _world(tmpdir)
        client = _FakeClient({"display": [], "state_delta": {}, "new_events": []})

        result = WorldSimulation(store, model_client=client).step(world_id, node_id, "我发呆。")

        assert result.ai_used is False
        assert result.display[-1]["type"] == "system_note"
        assert len(result.events) == 2


def test_model_exception_degrades_to_rule_fallback():
    with tempfile.TemporaryDirectory() as tmpdir:
        store, world_id, node_id = _world(tmpdir)
        client = _FakeClient(RuntimeError("upstream 502"))

        result = WorldSimulation(store, model_client=client).step(world_id, node_id, "我等待。")

        assert result.ai_used is False
        assert result.display[-1]["type"] == "system_note"


def test_no_model_client_never_calls_ai():
    with tempfile.TemporaryDirectory() as tmpdir:
        store, world_id, node_id = _world(tmpdir)

        result = WorldSimulation(store).step(world_id, node_id, "我观察。")

        assert result.ai_used is False
        assert result.events[0]["type"] == "player_action"
