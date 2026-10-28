import pytest
from flask import Flask

from nbot.web.message_pagination import message_page
from nbot.web.routes.sessions import register_session_routes


class Server:
    def __init__(self, data_dir, sessions):
        self.data_dir = str(data_dir)
        self.base_dir = str(data_dir)
        self.sessions = sessions
        self.personality = {}
        self._sessions_cache = []
        self._sessions_cache_time = 0

    def _save_data(self, _name):
        pass

    def _invalidate_sessions_cache(self):
        self._sessions_cache = []
        self._sessions_cache_time = 0


def _client(sessions, data_dir):
    app = Flask(__name__)
    server = Server(data_dir, sessions)
    register_session_routes(app, server)
    return app.test_client(), server


def test_pages_are_ordered_and_do_not_repeat_cursors():
    messages = [{"id": str(i), "role": "user", "content": str(i)} for i in range(9)]
    first = message_page(messages, 4)
    second = message_page(messages, 4, first["messages"][0]["id"])
    last = message_page(messages, 4, second["messages"][0]["id"])
    assert [m["id"] for m in first["messages"]] == ["5", "6", "7", "8"]
    assert [m["id"] for m in second["messages"]] == ["1", "2", "3", "4"]
    assert last == {"messages": [messages[0]], "has_more": False}
    assert first["has_more"] and second["has_more"]


@pytest.mark.parametrize("limit", [0, -1, 201, "no"])
def test_invalid_limits_are_rejected(limit):
    with pytest.raises(ValueError):
        message_page([], limit)


def test_route_keeps_legacy_shape_and_filters_hidden_messages(tmp_path):
    client, _ = _client({"session-1": {
        "id": "session-1", "type": "web", "name": "Paging",
        "messages": [
            {"id": "sys", "role": "system", "content": "system"},
            {"id": "a", "role": "user", "content": "first"},
            {"id": "gone", "role": "assistant", "content": "gone", "deleted": True},
            {"id": "b", "role": "assistant", "content": "second"},
        ],
    }}, tmp_path)
    assert isinstance(client.get("/api/sessions/session-1/messages").get_json(), list)
    detail = client.get("/api/sessions/session-1?include_messages=false").get_json()
    assert "messages" not in detail
    assert detail["message_count"] == 3
    assert "messages" in client.get("/api/sessions/session-1").get_json()
    page = client.get("/api/sessions/session-1/messages?limit=1").get_json()
    assert [item["id"] for item in page["messages"]] == ["b"]
    assert page["has_more"]
    assert client.get("/api/sessions/session-1/messages?limit=0").status_code == 400
    assert client.get(
        "/api/sessions/session-1/messages?limit=1&before_message_id=gone"
    ).status_code == 404
