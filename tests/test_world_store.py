import tempfile
import unittest

from nbot.world import WorldStore
from nbot.world.state_diff import apply_state_delta


class WorldStoreTest(unittest.TestCase):
    def test_create_world_and_record_step_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world(
                {
                    "name": "雾海边境",
                    "genre": "奇幻",
                    "current_time": "旧王历 317年 秋月 12日 21:30",
                    "weather": "浓雾",
                    "tension": 64,
                    "main_plot": "第十三声即将出现",
                }
            )

            world = bundle["world"]
            node = bundle["current_node"]
            self.assertEqual(world["name"], "雾海边境")
            self.assertEqual(node["state"]["global_state"]["weather"], "浓雾")

            event = store.add_event(
                world["world_id"],
                node["node_id"],
                "state_change",
                "封印稳定度下降。",
                state_delta={"global_state": {"tension": "+4"}},
            )
            self.assertEqual(event["type"], "state_change")

            updated = store.get_world_bundle(world["world_id"])
            self.assertEqual(updated["current_node"]["state"]["global_state"]["tension"], 68)
            self.assertEqual(len(updated["events"]), 1)

    def test_branch_from_event_creates_independent_node(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world({"name": "分支测试"})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]
            event = store.add_event(world_id, node_id, "world_event", "岔路出现。")

            branched = store.branch_from_event(world_id, event["event_id"], "调查档案馆")
            self.assertNotEqual(branched["current_node"]["node_id"], node_id)
            self.assertEqual(branched["current_node"]["parent_node_id"], node_id)
            self.assertEqual(branched["current_node"]["branch_name"], "调查档案馆")
            self.assertEqual(len(store.get_tree(world_id)), 2)

    def test_node_management_and_export_import(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = WorldStore(tmpdir)
            bundle = store.create_world({"name": "导出测试", "current_time": "第一天"})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]
            event = store.add_event(world_id, node_id, "world_event", "雾气升起。")
            branched = store.branch_from_event(world_id, event["event_id"], "港口线")
            branch_id = branched["current_node"]["node_id"]

            renamed = store.update_node(
                world_id, branch_id, {"branch_name": "灯塔线", "title": "灯塔线"}
            )
            self.assertEqual(renamed["branch_name"], "灯塔线")

            summary = store.summarize_node(world_id, branch_id)
            self.assertTrue(summary["summary"])

            exported = store.export_world(world_id)
            self.assertEqual(len(exported["nodes"]), 2)
            imported = store.import_world(exported)
            self.assertEqual(imported["world"]["name"], "导出测试")
            self.assertEqual(imported["counts"]["node_count"], 2)

            deleted = store.delete_node(world_id, branch_id)
            self.assertEqual(deleted["current_node"]["node_id"], node_id)
            self.assertEqual(len(store.get_tree(world_id)), 1)

    def test_apply_state_delta_supports_increments_and_append(self):
        state = {
            "global_state": {"tension": 10},
            "characters": {"char_luna": {"memory": ["初见"]}},
        }
        result = apply_state_delta(
            state,
            {
                "global_state": {"tension": "+5"},
                "characters": {"char_luna": {"memory_append": ["询问钟声"]}},
            },
        )
        self.assertEqual(result["global_state"]["tension"], 15)
        self.assertEqual(result["characters"]["char_luna"]["memory"], ["初见", "询问钟声"])


if __name__ == "__main__":
    unittest.main()
