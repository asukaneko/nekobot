import tempfile
import unittest

from flask import Flask
from flask_socketio import SocketIO

from nbot.web.world_socket_events import register_world_socket_events
from nbot.world import WorldStore


class WorldSocketEventsTest(unittest.TestCase):
    def test_join_and_world_action_emit_updates(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            app = Flask(__name__)
            app.config["SECRET_KEY"] = "test"
            socketio = SocketIO(app, async_mode="threading")
            store = WorldStore(tmpdir)
            server = type(
                "Server",
                (),
                {
                    "data_dir": tmpdir,
                    "socketio": socketio,
                    "world_store": store,
                    "ai_client": None,
                },
            )()
            register_world_socket_events(server)

            bundle = store.create_world({"name": "Socket World"})
            world_id = bundle["world"]["world_id"]
            node_id = bundle["current_node"]["node_id"]

            client = socketio.test_client(app)
            self.assertTrue(client.is_connected())

            client.emit("join_world", {"world_id": world_id, "node_id": node_id})
            received = client.get_received()
            self.assertTrue(any(item["name"] == "joined_world" for item in received))

            client.emit(
                "world_action",
                {"world_id": world_id, "node_id": node_id, "content": "I listen."},
            )
            received = client.get_received()
            event_names = [item["name"] for item in received]
            self.assertIn("world_state_updated", event_names)
            self.assertIn("world_event_created", event_names)


if __name__ == "__main__":
    unittest.main()
