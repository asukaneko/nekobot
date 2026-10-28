"""全局搜索后端（nbot/web/routes/global_search.py）测试。"""

import json
from types import SimpleNamespace

from flask import Flask

from nbot.web.routes import global_search
from nbot.web.routes.global_search import register_global_search_routes, search_all


class _StubSessionStore:
    def __init__(self, sessions):
        self._sessions = sessions

    def iter_sessions(self):
        return self._sessions.items()


class StubServer:
    """最小 server 替身：只暴露 base_dir / data_dir / sessions / memories。"""

    def __init__(self, base_dir, data_dir=None, sessions=None, memories=None):
        self.base_dir = str(base_dir)
        self.data_dir = str(data_dir or base_dir)
        self.sessions = dict(sessions or {})
        self.session_store = _StubSessionStore(self.sessions)
        self.memories = list(memories or [])
        self.custom_personality_presets = []


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_client(tmp_path, sessions=None, memories=None):
    app = Flask(__name__)
    server = StubServer(tmp_path, sessions=sessions, memories=memories)
    register_global_search_routes(app, server)
    return app.test_client(), server


def _search(client, **params):
    response = client.get("/api/search", query_string=params)
    return response.status_code, response.get_json()


def _sessions():
    return {
        "s1": {
            "id": "s1",
            "name": "星舰导航会议",
            "type": "web",
            "user_id": "u1",
            "tags": ["工作"],
            "messages": [
                {"id": "m1", "role": "user", "content": "今天去海边散步吗"},
                {"id": "m2", "role": "assistant", "content": "海风很舒服，我们出发吧"},
            ],
        },
        "s2": {
            "id": "s2",
            "name": "深夜闲聊",
            "type": "web",
            "messages": [
                {"id": "m3", "role": "user", "content": "给我讲个灯塔的故事吧"},
            ],
        },
    }


_CHARACTERS = [
    {
        "id": "c1",
        "name": "夜航星",
        "description": "沉默的领航员，擅长星图推演",
        "tags": ["科幻", "领航"],
        "created_at": "2026-05-01T00:00:00",
    },
    {
        "id": "c2",
        "name": "蜜柑",
        "description": "爱吃甜食的猫娘",
        "tags": ["日常"],
    },
]


def _world_books():
    return {
        "world_books": {
            "book1": {
                "id": "book1",
                "name": "星海纪元",
                "description": "银河编年史",
                "entries": {
                    "e1": {
                        "id": "e1",
                        "name": "灯塔公约",
                        "keywords": ["灯塔", "守夜人"],
                        "content": "所有灯塔必须彻夜点灯。",
                        "enabled": True,
                        "priority": 10,
                    }
                },
            }
        }
    }


def _memories():
    return [
        {
            "id": "mem1",
            "title": "灯塔守夜",
            "summary": "每周三守夜",
            "content": "用户答应每周三去灯塔守夜。",
            "target_id": "asuka",
            "character_name": "飞鸟",
        },
        {
            "id": "mem2",
            "title": "咖啡口味",
            "summary": "喜欢浅烘",
            "content": "用户偏爱浅烘焙咖啡。",
        },
    ]


# ----------------------------------------------------------------------
# 角色卡
# ----------------------------------------------------------------------


def test_character_preset_hit(tmp_path):
    _write_json(tmp_path / "custom_personality_presets.json", _CHARACTERS)
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="星图", types="character")

    assert status == 200
    items = body["groups"]["character"]["items"]
    assert [item["id"] for item in items] == ["c1"]
    assert items[0]["title"] == "夜航星"
    assert items[0]["route"] == {"page": "characters", "character_id": "c1"}
    assert "星图" in items[0]["snippet"]
    assert body["total"] == 1


def test_character_preset_miss(tmp_path):
    _write_json(tmp_path / "custom_personality_presets.json", _CHARACTERS)
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="吸血鬼", types="character")

    assert status == 200
    assert body["groups"]["character"]["items"] == []
    assert body["total"] == 0


def test_character_profiles_fallback(tmp_path):
    _write_json(
        tmp_path / "data" / "character" / "profiles.json",
        {"夜航星": {"id": "p1", "name": "夜航星", "description": "旧版快照：沉默领航员"}},
    )
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="领航员", types="character")

    assert status == 200
    assert [item["id"] for item in body["groups"]["character"]["items"]] == ["p1"]


def test_character_profiles_do_not_duplicate_preset_names(tmp_path):
    _write_json(tmp_path / "custom_personality_presets.json", _CHARACTERS)
    _write_json(
        tmp_path / "data" / "character" / "profiles.json",
        {"夜航星": {"id": "p1", "name": "夜航星", "description": "运行时快照"}},
    )
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="夜航星", types="character")

    assert status == 200
    assert [item["id"] for item in body["groups"]["character"]["items"]] == ["c1"]


# ----------------------------------------------------------------------
# 世界书
# ----------------------------------------------------------------------


def test_world_book_hit_returns_book_and_entry_ids(tmp_path):
    _write_json(tmp_path / "data" / "world_books.json", _world_books())
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="灯塔", types="world_book")

    assert status == 200
    items = body["groups"]["world_book"]["items"]
    assert [item["entry_id"] for item in items] == ["e1"]
    assert items[0]["book_id"] == "book1"
    assert items[0]["route"] == {"page": "world-book", "book_id": "book1", "entry_id": "e1"}
    assert "灯塔" in items[0]["snippet"]


def test_world_book_miss(tmp_path):
    _write_json(tmp_path / "data" / "world_books.json", _world_books())
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="海盗", types="world_book")

    assert status == 200
    assert body["groups"]["world_book"]["items"] == []


# ----------------------------------------------------------------------
# 记忆
# ----------------------------------------------------------------------


def test_memory_json_hit(tmp_path):
    _write_json(tmp_path / "data" / "memories.json", _memories())
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="守夜", types="memory")

    assert status == 200
    items = body["groups"]["memory"]["items"]
    assert [item["id"] for item in items] == ["mem1"]
    assert items[0]["subtitle"] == "飞鸟"
    assert items[0]["route"] == {"page": "memory", "memory_id": "mem1"}


def test_memory_json_miss(tmp_path):
    _write_json(tmp_path / "data" / "memories.json", _memories())
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="魔法阵", types="memory")

    assert status == 200
    assert body["groups"]["memory"]["items"] == []


def test_memory_fs_index_is_searched(tmp_path):
    _write_json(
        tmp_path / "memory_fs.json",
        {
            "characters/asuka/general.md": {
                "path": "characters/asuka/general.md",
                "character_id": "asuka",
                "title": "守夜人笔记",
                "summary": "",
                "content": "灯塔的光需要每夜维护。",
                "tags": [],
                "updated_at": "2026-05-01T00:00:00",
            }
        },
    )
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="灯塔", types="memory")

    assert status == 200
    items = body["groups"]["memory"]["items"]
    assert [item["id"] for item in items] == ["characters/asuka/general.md"]
    assert items[0]["route"] == {"page": "memory", "path": "characters/asuka/general.md"}


# ----------------------------------------------------------------------
# 会话 / 消息
# ----------------------------------------------------------------------


def test_session_name_hit(tmp_path):
    client, _ = _make_client(tmp_path, sessions=_sessions())

    status, body = _search(client, q="星舰", types="session")

    assert status == 200
    items = body["groups"]["session"]["items"]
    assert [item["id"] for item in items] == ["s1"]
    assert items[0]["title"] == "星舰导航会议"
    assert items[0]["route"] == {"page": "chat", "session_id": "s1"}


def test_message_content_hit(tmp_path):
    client, _ = _make_client(tmp_path, sessions=_sessions())

    status, body = _search(client, q="灯塔", types="message")

    assert status == 200
    items = body["groups"]["message"]["items"]
    assert [item["id"] for item in items] == ["m3"]
    assert items[0]["title"] == "深夜闲聊"
    assert items[0]["subtitle"] == "用户"
    assert items[0]["session_id"] == "s2"
    assert items[0]["route"] == {"page": "chat", "session_id": "s2", "message_id": "m3"}
    assert "灯塔" in items[0]["snippet"]


def test_session_from_db_when_memory_empty(tmp_path):
    from nbot.web.sessions_db import save_sessions

    save_sessions(
        str(tmp_path),
        {"s9": {"id": "s9", "name": "数据库会话", "type": "web", "messages": []}},
    )
    app = Flask(__name__)
    server = SimpleNamespace(base_dir=str(tmp_path), data_dir=str(tmp_path))
    register_global_search_routes(app, server)

    status, body = _search(app.test_client(), q="数据库", types="session")

    assert status == 200
    assert [item["id"] for item in body["groups"]["session"]["items"]] == ["s9"]


# ----------------------------------------------------------------------
# 参数与容错
# ----------------------------------------------------------------------


def test_types_filter_keeps_only_requested_groups(tmp_path):
    _write_json(tmp_path / "custom_personality_presets.json", _CHARACTERS)
    _write_json(tmp_path / "data" / "world_books.json", _world_books())
    _write_json(tmp_path / "data" / "memories.json", _memories())
    client, _ = _make_client(tmp_path, sessions=_sessions())

    status, body = _search(client, q="灯塔", types="session,message")

    assert status == 200
    assert set(body["groups"]) == {"session", "message"}
    assert body["groups"]["message"]["items"]
    assert body["total"] == 1


def test_empty_query_returns_400(tmp_path):
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="   ")

    assert status == 400
    assert body["success"] is False
    assert body["error"] == "query is required"
    assert body["groups"] == {}


def test_too_long_query_returns_400(tmp_path):
    client, _ = _make_client(tmp_path)

    status, body = _search(client, q="x" * 101)

    assert status == 400
    assert body["success"] is False
    assert "too long" in body["error"]


def test_source_error_is_recorded_without_breaking_others(tmp_path, monkeypatch):
    client, _ = _make_client(tmp_path, sessions=_sessions())

    def _boom(server, query_lower, limit, errors):
        raise RuntimeError("character store exploded")

    monkeypatch.setitem(global_search._SEARCH_HANDLERS, "character", _boom)

    status, body = _search(client, q="星舰", types="session,character")

    assert status == 200
    assert body["errors"] == ["character: character store exploded"]
    assert body["groups"]["session"]["items"]
    assert body["groups"]["character"]["items"] == []
    assert body["total"] == 1


def test_limit_is_clamped(tmp_path):
    sessions = {
        f"s{index}": {"id": f"s{index}", "name": f"测试会话 {index}", "type": "web", "messages": []}
        for index in range(60)
    }
    client, _ = _make_client(tmp_path, sessions=sessions)

    _, capped = _search(client, q="测试", types="session", limit=1000)
    assert len(capped["groups"]["session"]["items"]) == 50

    _, floored = _search(client, q="测试", types="session", limit=0)
    assert len(floored["groups"]["session"]["items"]) == 1
    assert floored["total"] == 1

    _, defaulted = _search(client, q="测试", types="session", limit="abc")
    assert len(defaulted["groups"]["session"]["items"]) == 10


def test_server_without_session_attributes_degrades_gracefully(tmp_path):
    app = Flask(__name__)
    server = SimpleNamespace(base_dir=str(tmp_path), data_dir=str(tmp_path))
    register_global_search_routes(app, server)

    status, body = _search(app.test_client(), q="任意查询")

    assert status == 200
    assert body["success"] is True
    assert body["groups"]["session"]["items"] == []
    assert body["errors"] == []


def test_search_all_returns_stable_payload(tmp_path):
    server = StubServer(tmp_path, sessions=_sessions())

    result = search_all(server, "星舰")

    assert result["success"] is True
    assert result["query"] == "星舰"
    assert set(result["groups"]) == {
        "session",
        "message",
        "character",
        "world_book",
        "memory",
    }
    assert isinstance(result["took_ms"], int)
    assert result["total"] == sum(len(group["items"]) for group in result["groups"].values())


# ----------------------------------------------------------------------
# 相关度排序 / 扫描范围
# ----------------------------------------------------------------------


def test_messages_ranked_by_relevance(tmp_path):
    sessions = {
        "s1": {
            "id": "s1",
            "name": "会话一",
            "type": "web",
            "updated_at": "2026-05-02T00:00:00",
            "messages": [
                {"id": "m1", "role": "user", "content": "灯塔灯塔灯塔，今晚灯塔要亮着"},
                {"id": "m2", "role": "user", "content": "顺路提一句灯塔"},
            ],
        },
    }
    client, _ = _make_client(tmp_path, sessions=sessions)

    status, body = _search(client, q="灯塔", types="message")

    assert status == 200
    items = body["groups"]["message"]["items"]
    assert [item["id"] for item in items] == ["m1", "m2"]
    assert items[0]["score"] > items[1]["score"]


def test_message_scan_limit_request_param(tmp_path):
    sessions = {
        "new": {
            "id": "new",
            "name": "新会话",
            "type": "web",
            "updated_at": "2026-05-03T00:00:00",
            "messages": [{"id": "n1", "role": "user", "content": "靠港检查"}],
        },
        "old": {
            "id": "old",
            "name": "旧会话",
            "type": "web",
            "updated_at": "2026-05-01T00:00:00",
            "messages": [{"id": "o1", "role": "user", "content": "靠港检查"}],
        },
    }
    client, _ = _make_client(tmp_path, sessions=sessions)

    _, unlimited = _search(client, q="靠港", types="message")
    assert [item["id"] for item in unlimited["groups"]["message"]["items"]] == ["n1", "o1"]
    assert unlimited["scan"] == {"max_sessions": 0, "max_messages_per_session": 0}

    _, limited = _search(client, q="靠港", types="message", max_sessions=1)
    assert [item["id"] for item in limited["groups"]["message"]["items"]] == ["n1"]
    assert limited["scan"]["max_sessions"] == 1


def test_message_scan_limit_from_settings(tmp_path):
    sessions = {
        "s1": {
            "id": "s1",
            "name": "会话",
            "type": "web",
            "messages": [
                {"id": "m1", "role": "user", "content": "最早靠港"},
                {"id": "m2", "role": "user", "content": "再次靠港"},
            ],
        },
    }
    client, server = _make_client(tmp_path, sessions=sessions)
    server.settings = {"global_search": {"max_messages_per_session": 1}}

    _, body = _search(client, q="靠港", types="message")

    assert body["scan"]["max_messages_per_session"] == 1
    assert [item["id"] for item in body["groups"]["message"]["items"]] == ["m2"]
