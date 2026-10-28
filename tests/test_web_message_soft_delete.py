import tempfile

from flask import Flask

from nbot.core.session_store import WebSessionStore, is_message_deleted, visible_messages
from nbot.web.routes.sessions import register_session_routes


class Server:
    def __init__(self, data_dir, sessions):
        self.data_dir = data_dir
        self.base_dir = data_dir
        self.sessions = sessions
        self.personality = {}
        self._sessions_cache = []
        self._sessions_cache_time = 0

    def _save_data(self, _name):
        pass

    def _invalidate_sessions_cache(self):
        self._sessions_cache = []
        self._sessions_cache_time = 0


def _build_session():
    return {
        "id": "session-1",
        "name": "Soft delete",
        "type": "web",
        "system_prompt": "sys",
        "messages": [
            {"id": "m-sys", "role": "system", "content": "sys"},
            {"id": "m-user-1", "role": "user", "content": "hello"},
            {"id": "m-assistant-1", "role": "assistant", "content": "hi"},
        ],
    }


def _client(sessions):
    app = Flask(__name__)
    server = Server(tempfile.gettempdir(), sessions)
    register_session_routes(app, server)
    return app.test_client(), server


def test_delete_message_marks_deleted_and_hides_from_responses():
    sessions = {"session-1": _build_session()}
    client, _server = _client(sessions)

    response = client.delete("/api/sessions/session-1/messages/m-user-1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["message"]["deleted"] is True
    assert payload["message_count"] == 2

    stored = sessions["session-1"]["messages"]
    assert len(stored) == 3
    assert is_message_deleted(stored[1])

    detail = client.get("/api/sessions/session-1").get_json()
    assert [m["id"] for m in detail["messages"]] == ["m-sys", "m-assistant-1"]
    assert detail["message_count"] == 2

    messages = client.get("/api/sessions/session-1/messages").get_json()
    assert [m["id"] for m in messages] == ["m-assistant-1"]

    listing = client.get("/api/sessions").get_json()
    assert listing[0]["message_count"] == 2


def test_deleted_message_cannot_be_edited_or_regenerated():
    sessions = {"session-1": _build_session()}
    client, _server = _client(sessions)

    client.delete("/api/sessions/session-1/messages/m-assistant-1")

    update = client.put(
        "/api/sessions/session-1/messages/m-assistant-1",
        json={"content": "rewrite"},
    )
    assert update.status_code == 404

    regenerate = client.post(
        "/api/sessions/session-1/regenerate",
        json={"message_id": "m-assistant-1"},
    )
    assert regenerate.status_code == 404
    assert regenerate.get_json()["success"] is False


def test_store_does_not_resurrect_deleted_message():
    sessions = {"session-1": _build_session()}
    store = WebSessionStore(sessions, save_callback=None)
    message = sessions["session-1"]["messages"][1]
    message["deleted"] = True
    message["deleted_at"] = "2026-09-27T00:00:00+08:00"

    store.append_message(
        "session-1",
        {"id": "m-user-1", "role": "user", "content": "stale write-back"},
    )
    assert is_message_deleted(store.get_messages("session-1")[1])
    assert store.get_messages("session-1")[1]["content"] == "hello"

    store.replace_messages(
        "session-1",
        [
            {"id": "m-sys", "role": "system", "content": "sys"},
            {"id": "m-user-1", "role": "user", "content": "stale copy"},
            {"id": "m-assistant-1", "role": "assistant", "content": "hi"},
        ],
    )
    assert [m["id"] for m in store.get_messages("session-1")] == [
        "m-sys",
        "m-assistant-1",
    ]
    assert [m["id"] for m in visible_messages(store.get_messages("session-1"))] == [
        "m-sys",
        "m-assistant-1",
    ]
