import json
import tempfile
import unittest

from nbot.world import WorldSimulation, WorldStore


class WorldEngineTest(unittest.TestCase):
    def test_step_uses_structured_ai_output_and_updates_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world({"name": "雾海边境", "tension": 10})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]

            def fake_model(messages):
                self.assertEqual(messages[0]["role"], "system")
                self.assertIn("user_action", messages[1]["content"])
                return json.dumps(
                    {
                        "display": [
                            {
                                "type": "narration",
                                "content": "雾从钟塔底部漫上来。",
                            },
                            {
                                "type": "dialogue",
                                "actor_id": "char_luna",
                                "actor_name": "露娜",
                                "content": "你来得太早了。",
                            },
                        ],
                        "state_delta": {"global_state": {"tension": "+5"}},
                        "new_events": [
                            {
                                "type": "world_event",
                                "title": "钟声",
                                "content": "远处响起一声低沉钟鸣。",
                            }
                        ],
                        "suggested_actions": ["追问露娜"],
                        "branch_hint": "是否进入钟塔适合创建分支。",
                    },
                    ensure_ascii=False,
                )

            result = WorldSimulation(store, model_client=fake_model).step(
                world_id, node_id, "我走近钟塔。"
            )

            self.assertTrue(result.ai_used)
            self.assertEqual(result.state["global_state"]["tension"], 15)
            self.assertEqual(result.suggested_actions, ["追问露娜"])
            self.assertEqual(len(result.events), 4)

    def test_step_falls_back_when_ai_returns_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world({"name": "雾海边境"})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]

            result = WorldSimulation(store, model_client=lambda messages: "不是 JSON").step(
                world_id, node_id, "我观察四周。"
            )

            self.assertFalse(result.ai_used)
            self.assertEqual(len(result.events), 2)
            self.assertEqual(result.display[-1]["type"], "system_note")

    def test_step_cleans_ai_payload_before_writing_events(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world({"name": "Schema World"})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]

            def fake_model(messages):
                return json.dumps(
                    {
                        "display": [
                            {"type": "bad_type", "content": "  still visible  "},
                            {"type": "dialogue", "content": ""},
                        ],
                        "state_delta": {"global_state": {"tension": "+3"}},
                        "new_events": [{"type": "bad_event", "content": "record me"}],
                        "suggested_actions": ["next", "next", 12],
                    }
                )

            result = WorldSimulation(store, model_client=fake_model).step(
                world_id, node_id, "act"
            )

            self.assertTrue(result.ai_used)
            self.assertEqual(result.display[0]["type"], "narration")
            self.assertEqual(result.display[0]["content"], "still visible")
            self.assertEqual(result.suggested_actions, ["next", "12"])
            self.assertEqual(result.events[-1]["type"], "world_event")

    def test_step_falls_back_when_ai_payload_has_no_effect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world({"name": "Empty AI World"})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]

            result = WorldSimulation(store, model_client=lambda messages: "{}").step(
                world_id, node_id, "act"
            )

            self.assertFalse(result.ai_used)
            self.assertEqual(result.display[-1]["type"], "system_note")

    def test_tick_advances_accelerated_world_without_ai(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world(
                {"name": "雾海边境", "time_mode": "accelerated", "time_scale": 10}
            )
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]

            result = WorldSimulation(store).tick(world_id, node_id, elapsed_seconds=60)

            self.assertFalse(result.ai_used)
            self.assertIn("elapsed_seconds_total", result.state["world_time"])
            self.assertEqual(result.state["world_time"]["elapsed_seconds_total"], 600)

    def test_jump_time_updates_world_time_without_ai(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world({"name": "雾海边境"})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]

            result = WorldSimulation(store).jump_time(world_id, node_id, "三天后 黄昏")

            self.assertEqual(result.state["world_time"]["display"], "三天后 黄昏")


if __name__ == "__main__":
    unittest.main()
