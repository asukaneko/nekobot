"""Tests for the data export center (nbot/web/routes/export_center.py)."""

import io
import json
import os
import re
import zipfile
from copy import deepcopy

import pytest
from flask import Flask

from nbot.character.storage.world_book_store import WorldBookStore
from nbot.web.routes import export_center
from nbot.web.routes.export_center import (
    build_export_zip,
    import_export_zip,
    list_export_categories,
    register_export_center_routes,
)


class _FakeSessionStore:
    """Minimal stand-in for WebSessionStore (list_sessions/get_session/set_session)."""

    def __init__(self):
        self.sessions = {}

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def set_session(self, session_id, session):
        self.sessions[session_id] = session
        return session

    def iter_sessions(self):
        return self.sessions.items()


class _StubServer:
    def __init__(self, root, sessions=None):
        self.base_dir = str(root)
        self.data_dir = str(root)
        self.settings = {}
        self.session_store = _FakeSessionStore()
        self.sessions = self.session_store.sessions
        for session_id, session in (sessions or {}).items():
            self.session_store.sessions[session_id] = session
        self.custom_personality_presets = []
        self.memories = []
        self.workflows = []
        self.logs = []

    def log_message(self, level, message, important=False):
        self.logs.append((level, message, important))


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _zip_with(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in entries.items():
            if isinstance(payload, (dict, list)):
                payload = json.dumps(payload, ensure_ascii=False)
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            archive.writestr(name, payload)
    return buffer.getvalue()


def _manifest(categories):
    return {
        "format": "nekobot-export",
        "version": 1,
        "created_at": "2026-01-02T03:04:05+08:00",
        "app": "NcatBot",
        "categories": categories,
    }


def _seed(root):
    presets = [{"id": "c1", "name": "角色一", "tags": ["测试"]}]
    memories = [{"id": "m1", "title": "记忆一"}, {"id": "m2", "title": "记忆二"}]
    hooks = [{"id": "h1", "name": "hook", "event": "model.before_call", "actions": []}]
    workflows = [{"id": "w1", "name": "工作流", "nodes": [], "edges": []}]
    settings = {"theme": "dark", "lang": "zh"}

    _write_json(os.path.join(str(root), "settings.json"), settings)
    _write_json(os.path.join(str(root), "custom_personality_presets.json"), presets)
    _write_json(os.path.join(str(root), "memories.json"), memories)
    _write_json(os.path.join(str(root), "hooks.json"), hooks)
    _write_json(os.path.join(str(root), "workflows.json"), workflows)

    sessions = {
        "s1": {
            "id": "s1",
            "name": "会话一",
            "messages": [{"role": "user", "content": "你好"}],
        },
        "s2": {"id": "s2", "name": "会话二", "messages": []},
    }

    server = _StubServer(root, sessions=sessions)
    server.settings = settings
    server.custom_personality_presets = deepcopy(presets)
    server.memories = deepcopy(memories)
    server.workflows = deepcopy(workflows)

    store = WorldBookStore(str(root))
    book = store.create("世界观", description="测试世界书")
    store.add_entry(book.id, {"name": "入口", "keywords": ["灯塔", "海雾"], "content": "内容一"})
    store.add_entry(book.id, {"name": "港口", "keywords": ["港口"], "content": "内容二"})
    server.world_book_id = book.id
    return server


def _make_client(server):
    app = Flask(__name__)
    register_export_center_routes(app, server)
    return app.test_client()


# ---------------------------------------------------------------------------
# 分类清单
# ---------------------------------------------------------------------------


def test_list_export_categories_reports_real_counts(tmp_path):
    server = _seed(tmp_path)
    categories = {item["key"]: item for item in list_export_categories(server)}

    assert list(categories) == list(export_center.CATEGORY_ORDER)
    assert categories["sessions"]["count"] == 2
    assert categories["characters"]["count"] == 1
    assert categories["world_books"]["count"] == 1
    assert categories["memories"]["count"] == 2
    assert categories["settings"]["count"] == 2
    assert categories["hooks"]["count"] == 1
    assert categories["workflows"]["count"] == 1
    assert all(item["bytes"] > 0 for item in categories.values())
    assert all(item["label"] and item["description"] for item in categories.values())
    # 敏感数据不提供导出分类
    assert "api_keys" not in categories


def test_list_export_categories_never_raises(tmp_path):
    server = _StubServer(tmp_path)

    def boom():
        raise RuntimeError("broken store")

    server.session_store.iter_sessions = boom
    server.data_dir = str(tmp_path / "missing" / "nested")
    categories = {item["key"]: item for item in list_export_categories(server)}

    assert categories["sessions"]["count"] == 0
    assert categories["characters"]["count"] == 0
    assert categories["world_books"]["count"] == 0


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def test_export_zip_contains_manifest_and_category_files(tmp_path):
    server = _seed(tmp_path)
    blob = build_export_zip(server, ["sessions", "characters", "world_books"])

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        names = set(archive.namelist())
        assert names == {
            "manifest.json",
            "data/sessions.json",
            "data/characters.json",
            "data/world_books.json",
        }
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        assert manifest["format"] == "nekobot-export"
        assert manifest["version"] == 1
        assert manifest["app"] == "NcatBot"
        assert manifest["created_at"]
        assert manifest["categories"]["sessions"]["count"] == 2
        assert manifest["categories"]["characters"]["count"] == 1
        assert manifest["categories"]["world_books"]["count"] == 1
        assert manifest["categories"]["sessions"]["scope"] == "all"

        sessions = json.loads(archive.read("data/sessions.json").decode("utf-8"))
        assert {item["id"] for item in sessions} == {"s1", "s2"}
        books = json.loads(archive.read("data/world_books.json").decode("utf-8"))
        assert server.world_book_id in books["world_books"]


def test_export_does_not_mutate_sources(tmp_path):
    server = _seed(tmp_path)
    sessions_before = deepcopy(server.session_store.sessions)
    presets_before = deepcopy(server.custom_personality_presets)
    settings_before = deepcopy(server.settings)

    build_export_zip(server, list(export_center.CATEGORY_ORDER))

    assert server.session_store.sessions == sessions_before
    assert server.custom_personality_presets == presets_before
    assert server.settings == settings_before
    assert deepcopy(server.session_store.sessions) == sessions_before


# ---------------------------------------------------------------------------
# 导入：merge / overwrite
# ---------------------------------------------------------------------------


def _export_all(root):
    server = _seed(root)
    blob = build_export_zip(server, list(export_center.CATEGORY_ORDER))
    return server, blob


def test_import_merge_adds_into_empty_target(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()

    source, blob = _export_all(source_root)
    target = _StubServer(target_root)
    result = import_export_zip(target, blob)

    assert result["success"] is True
    assert result["mode"] == "merge"
    assert result["warnings"] == []

    imported = result["imported"]
    assert imported["sessions"] == {"added": 2, "updated": 0, "skipped": 0}
    assert imported["characters"] == {"added": 1, "updated": 0, "skipped": 0}
    assert imported["world_books"] == {"added": 1, "updated": 0, "skipped": 0}
    memories_stats = imported["memories"]
    assert (memories_stats["added"], memories_stats["updated"], memories_stats["skipped"]) == (2, 0, 0)
    assert memories_stats["entries"]["added"] == 2
    assert memories_stats["memory_fs"]["added"] == 0
    assert imported["settings"] == {"added": 1, "updated": 0, "skipped": 0}
    assert imported["hooks"] == {"added": 1, "updated": 0, "skipped": 0}
    assert imported["workflows"] == {"added": 1, "updated": 0, "skipped": 0}

    assert set(target.session_store.sessions) == {"s1", "s2"}
    assert target.custom_personality_presets == [{"id": "c1", "name": "角色一", "tags": ["测试"]}]
    assert target.settings == {"theme": "dark", "lang": "zh"}
    assert [item["id"] for item in target.memories] == ["m1", "m2"]
    assert [item["id"] for item in target.workflows] == ["w1"]
    assert len(WorldBookStore(str(target_root)).list_all()) == 1
    assert source.world_book_id


def test_import_merge_twice_skips_existing(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()

    _, blob = _export_all(source_root)
    target = _StubServer(target_root)
    import_export_zip(target, blob)
    second = import_export_zip(target, blob)

    assert second["warnings"] == []
    counts = second["imported"]
    for key in ("sessions", "characters", "world_books", "memories", "hooks", "workflows"):
        assert counts[key]["added"] == 0
        assert counts[key]["updated"] == 0
    assert counts["sessions"]["skipped"] == 2
    assert counts["characters"]["skipped"] == 1
    assert counts["world_books"]["skipped"] == 1
    assert counts["memories"]["skipped"] == 2
    assert counts["hooks"]["skipped"] == 1
    assert counts["workflows"]["skipped"] == 1
    assert counts["settings"] == {"added": 0, "updated": 0, "skipped": 1}
    assert set(target.session_store.sessions) == {"s1", "s2"}


def test_import_overwrite_updates_existing_entries(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()

    _, blob = _export_all(source_root)
    target = _StubServer(target_root)
    import_export_zip(target, blob)

    # 本地改坏数据，再由 overwrite 覆盖回来
    target.custom_personality_presets = [{"id": "c1", "name": "被改坏"}]
    _write_json(os.path.join(str(target_root), "custom_personality_presets.json"),
                target.custom_personality_presets)
    target.session_store.sessions["s1"]["name"] = "被改名"
    target.settings = {"theme": "light"}
    _write_json(os.path.join(str(target_root), "settings.json"), target.settings)

    result = import_export_zip(target, blob, mode="overwrite")
    counts = result["imported"]
    assert counts["sessions"]["updated"] == 2
    assert counts["characters"]["updated"] == 1
    assert counts["world_books"]["updated"] == 1
    assert counts["memories"]["updated"] == 2
    assert counts["hooks"]["updated"] == 1
    assert counts["workflows"]["updated"] == 1
    assert counts["settings"]["updated"] == 1
    assert counts["sessions"]["added"] == 0
    assert counts["characters"]["added"] == 0

    assert target.custom_personality_presets[0]["name"] == "角色一"
    assert target.session_store.sessions["s1"]["name"] == "会话一"
    assert target.settings == {"theme": "dark", "lang": "zh"}


def test_import_rejects_unknown_mode(tmp_path):
    server = _StubServer(tmp_path)
    blob = build_export_zip(_seed(tmp_path / "source"), ["sessions"])
    with pytest.raises(ValueError):
        import_export_zip(server, blob, mode="replace")


# ---------------------------------------------------------------------------
# 导入安全性
# ---------------------------------------------------------------------------


def test_import_rejects_zip_without_manifest(tmp_path):
    server = _StubServer(tmp_path)
    blob = _zip_with({"data/sessions.json": []})
    with pytest.raises(ValueError):
        import_export_zip(server, blob)
    assert sorted(os.listdir(str(tmp_path))) == []


def test_import_rejects_broken_zip(tmp_path):
    server = _StubServer(tmp_path)
    with pytest.raises(ValueError):
        import_export_zip(server, b"not a zip at all")


def test_import_ignores_path_traversal_and_unknown_categories(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    server = _StubServer(root)
    manifest = _manifest({"sessions": 1, "../evil": 1, "unknowncat": 1})
    blob = _zip_with(
        {
            "manifest.json": manifest,
            "data/sessions.json": [{"id": "s1", "name": "外部会话", "messages": []}],
            "../evil.json": {"pwn": True},
            "data/../../evil.json": {"pwn": True},
            "data/unknowncat.json": {"pwn": True},
        }
    )

    result = import_export_zip(server, blob)

    assert result["success"] is True
    assert "s1" in server.session_store.sessions
    assert result["imported"]["sessions"]["added"] == 1
    assert any("未知分类" in warning for warning in result["warnings"])
    # 没有向目标目录之外或之内写任何文件
    assert sorted(os.listdir(str(root))) == []
    assert not (tmp_path / "evil.json").exists()
    assert not (root.parent / "evil.json").exists()


def test_import_corrupt_json_category_only_warns(tmp_path):
    root = tmp_path / "target"
    root.mkdir()
    server = _StubServer(root)
    manifest = _manifest({"settings": 1, "characters": 1})
    blob = _zip_with(
        {
            "manifest.json": manifest,
            "data/settings.json": "{this is not json",
            "data/characters.json": [{"id": "c9", "name": "恢复的角色"}],
        }
    )

    result = import_export_zip(server, blob)

    assert result["success"] is True
    assert result["imported"]["settings"]["error"]
    assert result["warnings"]
    assert result["imported"]["characters"]["added"] == 1
    assert not os.path.exists(os.path.join(str(root), "settings.json"))
    assert server.custom_personality_presets == [{"id": "c9", "name": "恢复的角色"}]


def test_import_rejects_oversized_member(tmp_path, monkeypatch):
    monkeypatch.setattr(export_center, "MAX_SINGLE_FILE", 64)
    server = _StubServer(tmp_path)
    manifest = _manifest({"sessions": 1})
    blob = _zip_with(
        {
            "manifest.json": manifest,
            "data/sessions.json": [{"id": "s1", "name": "x" * 500}],
        }
    )
    with pytest.raises(ValueError):
        import_export_zip(server, blob)


# ---------------------------------------------------------------------------
# 世界书往返
# ---------------------------------------------------------------------------


def test_world_book_roundtrip_preserves_entries_and_keywords(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()

    store = WorldBookStore(str(source_root))
    book = store.create("深海灯塔", description="设定集")
    store.add_entry(book.id, {"name": "灯塔", "keywords": ["灯塔", "海雾"], "content": "a"})
    store.add_entry(book.id, {"name": "港口", "keywords": ["港口"], "content": "b"})
    source = _StubServer(source_root)

    blob = build_export_zip(source, ["world_books"])
    target = _StubServer(target_root)
    result = import_export_zip(target, blob)
    assert result["imported"]["world_books"] == {"added": 1, "updated": 0, "skipped": 0}

    target_store = WorldBookStore(str(target_root))
    books = target_store.list_all()
    assert len(books) == 1
    assert books[0].id == book.id
    assert books[0].name == "深海灯塔"
    assert books[0].description == "设定集"
    assert len(books[0].entries) == 2
    keywords = {frozenset(entry.keywords) for entry in books[0].entries}
    assert keywords == {frozenset(["港口"]), frozenset(["灯塔", "海雾"])}

    exported = json.loads(zipfile.ZipFile(io.BytesIO(blob)).read("data/world_books.json"))
    exported_entries = exported["world_books"][book.id]["entries"]
    source_entry_ids = {entry.id for entry in store.list_entries(book.id)}
    assert {entry["id"] for entry in exported_entries} == source_entry_ids
    assert {entry.id for entry in books[0].entries} == source_entry_ids


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_export_route_requires_selected_categories(tmp_path):
    server = _seed(tmp_path)
    client = _make_client(server)

    response = client.post("/api/export-center/export", json={"categories": []})
    assert response.status_code == 400
    assert response.get_json()["success"] is False

    response = client.post("/api/export-center/export", json={"categories": ["api_keys"]})
    assert response.status_code == 400


def test_routes_categories_export_and_import_roundtrip(tmp_path):
    source_root = tmp_path / "source"
    target_root = tmp_path / "target"
    source_root.mkdir()
    target_root.mkdir()

    source = _seed(source_root)
    client = _make_client(source)

    response = client.get("/api/export-center/categories")
    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert {item["key"] for item in body["categories"]} == set(export_center.CATEGORY_ORDER)

    response = client.post(
        "/api/export-center/export", json={"categories": ["sessions", "characters"]}
    )
    assert response.status_code == 200
    assert response.mimetype == "application/zip"
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment" in disposition
    assert re.search(r"nekobot-export-\d{8}-\d{6}\.zip", disposition)

    target = _StubServer(target_root)
    client = _make_client(target)

    response = client.post(
        "/api/export-center/import",
        data={"file": (io.BytesIO(response.data), "export.zip"), "mode": "merge"},
        content_type="multipart/form-data",
    )
    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["imported"]["sessions"] == {"added": 2, "updated": 0, "skipped": 0}
    assert body["imported"]["characters"] == {"added": 1, "updated": 0, "skipped": 0}
    assert target.logs


def test_import_route_without_file_returns_400(tmp_path):
    server = _seed(tmp_path)
    client = _make_client(server)

    response = client.post(
        "/api/export-center/import",
        data={"mode": "merge"},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_import_route_rejects_zip_without_manifest(tmp_path):
    server = _seed(tmp_path)
    client = _make_client(server)
    blob = _zip_with({"data/sessions.json": []})

    response = client.post(
        "/api/export-center/import",
        data={"file": (io.BytesIO(blob), "bad.zip")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.get_json()["success"] is False


# ---------------------------------------------------------------------------
# 会话：磁盘扫描（未加载会话）与分页
# ---------------------------------------------------------------------------


def test_sessions_scope_all_includes_unloaded_disk_sessions(tmp_path):
    from nbot.web.sessions_db import upsert_session

    server = _seed(tmp_path)
    upsert_session(
        str(tmp_path),
        "s3",
        {"id": "s3", "name": "磁盘会话", "messages": [], "updated_at": "2026-02-01T00:00:00"},
    )

    all_ids = {item["id"] for item in export_center._collect_sessions(server, scope="all")}
    assert all_ids == {"s1", "s2", "s3"}

    loaded_ids = {
        item["id"] for item in export_center._collect_sessions(server, scope="loaded")
    }
    assert loaded_ids == {"s1", "s2"}


def test_sessions_limit_keeps_most_recent(tmp_path):
    server = _seed(tmp_path)
    server.session_store.sessions["s1"]["updated_at"] = "2026-01-01T00:00:00"
    server.session_store.sessions["s2"]["updated_at"] = "2026-03-01T00:00:00"

    items = export_center._collect_sessions(server, scope="all", limit=1)
    assert [item["id"] for item in items] == ["s2"]


def test_sessions_reads_global_sessions_db(tmp_path):
    import sqlite3

    server = _seed(tmp_path)
    db_path = tmp_path / "data" / "sessions.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)")
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?)",
            ("g1", json.dumps({"id": "g1", "messages": []})),
        )

    ids = {item["id"] for item in export_center._collect_sessions(server, scope="all")}
    assert "g1" in ids


def test_sessions_invalid_scope_raises(tmp_path):
    server = _seed(tmp_path)
    with pytest.raises(ValueError):
        export_center._collect_sessions(server, scope="weird")


# ---------------------------------------------------------------------------
# 记忆：MemoryFS + memories.json 合并
# ---------------------------------------------------------------------------


def test_memories_export_merges_memory_fs(tmp_path):
    from nbot.memory.fs import MemoryFS

    server = _seed(tmp_path)
    MemoryFS(str(tmp_path)).write(
        "characters/c1/general.md",
        character_id="c1",
        title="常规",
        content="角色设定",
    )

    payload = export_center._collect_memories(server)
    assert len(payload["entries"]) == 2
    assert [item["path"] for item in payload["memory_fs"]] == ["characters/c1/general.md"]
    assert payload["sources"] == {"entries": 2, "memory_fs": 1}
    assert export_center._payload_count("memories", payload) == 3

    categories = {item["key"]: item for item in list_export_categories(server)}
    assert categories["memories"]["count"] == 3
    assert categories["memories"]["entries"] == 2
    assert categories["memories"]["memory_fs"] == 1


def test_memories_import_writes_both_channels(tmp_path):
    from nbot.memory.fs import MemoryFS

    server = _StubServer(tmp_path)
    payload = {
        "entries": [{"id": "m1", "title": "记忆一"}],
        "memory_fs": [
            {"path": "characters/c1/general.md", "character_id": "c1", "content": "内容一"}
        ],
    }
    stats = export_center._import_memories(server, payload, "merge")
    assert stats["added"] == 2
    assert stats["entries"]["added"] == 1
    assert stats["memory_fs"]["added"] == 1
    assert server.memories == [{"id": "m1", "title": "记忆一"}]
    assert MemoryFS(str(tmp_path)).read("characters/c1/general.md").content == "内容一"

    again = export_center._import_memories(server, payload, "merge")
    assert again["added"] == 0
    assert again["skipped"] == 2

    updated = export_center._import_memories(
        server,
        {"memory_fs": [{"path": "characters/c1/general.md", "character_id": "c1", "content": "内容二"}]},
        "overwrite",
    )
    assert updated["memory_fs"]["updated"] == 1
    assert MemoryFS(str(tmp_path)).read("characters/c1/general.md").content == "内容二"


def test_memories_import_skips_unsafe_paths(tmp_path):
    server = _StubServer(tmp_path)
    stats = export_center._import_memories(
        server,
        {
            "memory_fs": [
                {"path": "../evil.md"},
                {"path": "/abs.md"},
                {"path": "C:/win.md"},
                {"path": ""},
            ]
        },
        "overwrite",
    )
    assert stats["memory_fs"]["skipped"] == 4
    assert stats["added"] == 0


def test_memories_roundtrip_through_zip(tmp_path):
    from nbot.memory.fs import MemoryFS

    source_root = tmp_path / "src"
    target_root = tmp_path / "dst"
    source_root.mkdir()
    target_root.mkdir()
    source = _seed(source_root)
    MemoryFS(str(source_root)).write(
        "characters/c1/general.md", character_id="c1", content="往返内容"
    )

    blob = build_export_zip(source, ["memories"])
    target = _StubServer(target_root)
    result = import_export_zip(target, blob)

    assert result["imported"]["memories"]["added"] == 3
    restored = MemoryFS(str(target_root)).read("characters/c1/general.md")
    assert restored is not None and restored.content == "往返内容"


# ---------------------------------------------------------------------------
# manifest 明细与路由参数
# ---------------------------------------------------------------------------


def test_export_manifest_records_detail_and_scope(tmp_path):
    server = _seed(tmp_path)
    blob = build_export_zip(
        server, ["sessions", "memories"], sessions_scope="loaded", session_limit=1
    )

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        sessions = json.loads(archive.read("data/sessions.json").decode("utf-8"))
        memories = json.loads(archive.read("data/memories.json").decode("utf-8"))

    assert manifest["sessions_scope"] == "loaded"
    assert manifest["session_limit"] == 1
    assert manifest["categories"]["sessions"]["count"] == 1
    assert manifest["categories"]["sessions"]["scope"] == "loaded"
    assert manifest["categories"]["sessions"]["loaded"] == 2
    assert manifest["categories"]["memories"]["count"] == 2
    assert manifest["categories"]["memories"]["memory_fs"] == 0
    assert isinstance(memories, dict)
    assert len(memories["entries"]) == 2
    assert isinstance(sessions, list) and len(sessions) == 1


def test_route_categories_supports_sessions_scope(tmp_path):
    server = _seed(tmp_path)
    client = _make_client(server)

    ok = client.get("/api/export-center/categories?sessions_scope=loaded")
    assert ok.status_code == 200
    assert ok.get_json()["sessions_scope"] == "loaded"

    bad = client.get("/api/export-center/categories?sessions_scope=weird")
    assert bad.status_code == 400


def test_route_export_validates_session_limit(tmp_path):
    server = _seed(tmp_path)
    client = _make_client(server)

    assert (
        client.post(
            "/api/export-center/export",
            json={"categories": ["sessions"], "session_limit": "x"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/export-center/export",
            json={"categories": ["sessions"], "session_limit": -1},
        ).status_code
        == 400
    )

    ok = client.post(
        "/api/export-center/export",
        json={"categories": ["sessions"], "sessions_scope": "loaded", "session_limit": 1},
    )
    assert ok.status_code == 200


def test_route_export_invalid_scope_returns_400(tmp_path):
    server = _seed(tmp_path)
    client = _make_client(server)
    response = client.post(
        "/api/export-center/export",
        json={"categories": ["sessions"], "sessions_scope": "weird"},
    )
    assert response.status_code == 400


def test_import_result_reports_export_scope(tmp_path):
    source_root = tmp_path / "src"
    target_root = tmp_path / "dst"
    source_root.mkdir()
    target_root.mkdir()
    source = _seed(source_root)
    blob = build_export_zip(
        source, ["sessions"], sessions_scope="loaded", session_limit=1
    )

    result = import_export_zip(_StubServer(target_root), blob)
    assert result["sessions_scope"] == "loaded"
    assert result["session_limit"] == 1


def test_import_legacy_manifest_without_scope_fields(tmp_path):
    blob = _zip_with(
        {
            "manifest.json": _manifest({"sessions": 1}),
            "data/sessions.json": [{"id": "legacy-1", "messages": []}],
        }
    )
    result = import_export_zip(_StubServer(tmp_path), blob)
    assert result["success"] is True
    assert result["sessions_scope"] == ""
    assert result["session_limit"] == 0
    assert result["imported"]["sessions"]["added"] == 1
