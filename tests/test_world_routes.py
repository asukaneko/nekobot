import tempfile
import unittest
import json

from flask import Flask

from nbot.web.routes.worlds import register_world_routes


class WorldRoutesTest(unittest.TestCase):
    def test_world_crud_and_step_routes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = Flask(__name__)
            server = type("Server", (), {"data_dir": tmpdir, "world_store": None})()
            register_world_routes(app, server)
            client = app.test_client()

            create_response = client.post(
                "/api/worlds",
                json={
                    "name": "雾海边境",
                    "current_time": "旧王历 317年 秋月 12日",
                    "weather": "浓雾",
                },
            )
            self.assertEqual(create_response.status_code, 201)
            payload = create_response.get_json()
            world_id = payload["world"]["world_id"]
            node_id = payload["current_node"]["node_id"]

            list_response = client.get("/api/worlds")
            self.assertEqual(list_response.status_code, 200)
            self.assertEqual(len(list_response.get_json()["worlds"]), 1)

            character_response = client.post(
                f"/api/worlds/{world_id}/characters",
                json={"name": "露娜", "role": "钟塔守夜人"},
            )
            self.assertEqual(character_response.status_code, 201)
            character_id = character_response.get_json()["character"]["character_id"]

            import_character_response = client.post(
                f"/api/worlds/{world_id}/characters/import-card",
                json={"card": {"char_name": "伊琳", "occupation": "档案员"}},
            )
            self.assertEqual(import_character_response.status_code, 201)
            self.assertEqual(
                import_character_response.get_json()["characters"][0]["name"], "伊琳"
            )

            asset_response = client.post(
                f"/api/worlds/{world_id}/assets",
                json={"name": "黑钟塔", "asset_type": "location"},
            )
            self.assertEqual(asset_response.status_code, 201)
            asset_id = asset_response.get_json()["asset"]["asset_id"]

            step_response = client.post(
                f"/api/worlds/{world_id}/nodes/{node_id}/step",
                json={"content": "我走近钟塔。", "action_type": "act"},
            )
            self.assertEqual(step_response.status_code, 200)
            self.assertEqual(len(step_response.get_json()["events"]), 2)

            tree_response = client.get(f"/api/worlds/{world_id}/tree")
            self.assertEqual(tree_response.status_code, 200)
            self.assertEqual(len(tree_response.get_json()["tree"]), 1)

            jump_response = client.post(
                f"/api/worlds/{world_id}/nodes/{node_id}/jump-time",
                json={"target_time": "三天后"},
            )
            self.assertEqual(jump_response.status_code, 200)
            self.assertEqual(jump_response.get_json()["state"]["world_time"]["display"], "三天后")

            export_response = client.get(f"/api/worlds/{world_id}/export")
            self.assertEqual(export_response.status_code, 200)
            self.assertEqual(export_response.get_json()["export"]["world"]["world_id"], world_id)

            summarize_response = client.post(
                f"/api/worlds/{world_id}/nodes/{node_id}/summarize", json={}
            )
            self.assertEqual(summarize_response.status_code, 200)
            self.assertTrue(summarize_response.get_json()["node"]["summary"])

            rename_response = client.put(
                f"/api/worlds/{world_id}/nodes/{node_id}",
                json={"branch_name": "重命名主线", "title": "重命名主线"},
            )
            self.assertEqual(rename_response.status_code, 200)
            self.assertEqual(rename_response.get_json()["node"]["branch_name"], "重命名主线")

            import_response = client.post(
                "/api/worlds/import", json=export_response.get_json()["export"]
            )
            self.assertEqual(import_response.status_code, 201)
            self.assertNotEqual(import_response.get_json()["world"]["world_id"], world_id)

            self.assertEqual(
                client.delete(f"/api/worlds/{world_id}/characters/{character_id}").status_code,
                200,
            )
            self.assertEqual(
                client.delete(f"/api/worlds/{world_id}/assets/{asset_id}").status_code,
                200,
            )

    def test_import_card_route_accepts_tavern_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = Flask(__name__)
            server = type("Server", (), {"data_dir": tmpdir, "world_store": None})()
            register_world_routes(app, server)
            client = app.test_client()

            world_id = client.post("/api/worlds", json={"name": "雾海边境"}).get_json()["world"][
                "world_id"
            ]
            response = client.post(
                f"/api/worlds/{world_id}/characters/import-card",
                json={
                    "spec": "chara_card_v2",
                    "data": {
                        "name": "露娜",
                        "description": "钟塔守夜人",
                        "first_mes": "别靠近塔门。",
                        "character_book": {
                            "name": "钟塔秘录",
                            "entries": [{"keys": ["钟塔"], "content": "塔里没有钟声。"}],
                        },
                    },
                },
            )
            self.assertEqual(response.status_code, 201)
            character = response.get_json()["characters"][0]
            self.assertEqual(character["name"], "露娜")
            self.assertEqual(character["source_type"], "tavern")
            self.assertEqual(character["card"]["tavern_format"], "tavern_v2")
            self.assertEqual(character["card"]["character_book"]["name"], "钟塔秘录")

    def test_step_route_uses_ai_when_server_has_model_client(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = Flask(__name__)

            class Server:
                data_dir = tmpdir
                world_store = None
                ai_client = object()

                def _get_ai_response(self, messages):
                    return json.dumps(
                        {
                            "display": [{"type": "narration", "content": "雾气涌动。"}],
                            "state_delta": {"global_state": {"tension": "+2"}},
                            "suggested_actions": ["检查大门"],
                            "branch_hint": "进入前可分支。",
                        },
                        ensure_ascii=False,
                    )

            register_world_routes(app, Server())
            client = app.test_client()

            payload = client.post("/api/worlds", json={"name": "AI 世界", "tension": 3}).get_json()
            world_id = payload["world"]["world_id"]
            node_id = payload["current_node"]["node_id"]

            response = client.post(
                f"/api/worlds/{world_id}/nodes/{node_id}/step",
                json={"content": "我观察钟塔。"},
            )
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["ai_used"])
            self.assertEqual(data["state"]["global_state"]["tension"], 5)
            self.assertEqual(data["suggested_actions"], ["检查大门"])


if __name__ == "__main__":
    unittest.main()
