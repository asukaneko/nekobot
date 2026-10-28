"""WebDAV 增量修订 / 版本管理 / 资产同步测试（假 WebDAV 服务器，真实请求路径）。"""

import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from flask import Flask

from nbot.web import webdav_backup as wd
from nbot.web.routes.webdav_backup import register_webdav_backup_routes


class FakeRequestException(Exception):
    pass


class FakeResponse:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace") if content else ""
        self.headers = headers or {}


class FakeDAV:
    """极简 WebDAV 服务器：文件存 dict，支持 PUT/GET/DELETE/MKCOL/PROPFIND/HEAD。"""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.folders: set[str] = {"/"}
        self.calls: list[tuple[str, str]] = []

    def handle(self, method, url, auth=None, headers=None, data=None, **kwargs):
        path = urlparse(url).path
        self.calls.append((method, path))
        if method == "PROPFIND":
            return self._propfind(path, (headers or {}).get("Depth", "0"))
        if method == "MKCOL":
            if path in self.folders:
                return FakeResponse(405)
            parent = path.rstrip("/").rsplit("/", 1)[0] + "/"
            if parent not in self.folders:
                return FakeResponse(409)
            self.folders.add(path)
            return FakeResponse(201)
        if method == "PUT":
            self.files[path] = data or b""
            return FakeResponse(201)
        if method == "GET":
            if path not in self.files:
                return FakeResponse(404)
            return FakeResponse(200, self.files[path])
        if method == "DELETE":
            if path not in self.files:
                return FakeResponse(404)
            del self.files[path]
            return FakeResponse(200)
        if method == "HEAD":
            return FakeResponse(200 if path in self.files else 404)
        return FakeResponse(405)

    def _propfind(self, path, depth):
        has_children = any(item.startswith(path) and item != path for item in self.files)
        if path not in self.folders and not has_children:
            return FakeResponse(404)
        parts = ['<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">']
        if path in self.folders:
            parts.append(
                f"<D:response><D:href>{path}</D:href><D:propstat><D:prop>"
                "<D:resourcetype><D:collection/></D:resourcetype>"
                "<D:getlastmodified>Mon, 26 Oct 2026 10:00:00 GMT</D:getlastmodified>"
                "</D:prop></D:propstat></D:response>"
            )
        if depth != "0":
            for file_path, content in sorted(self.files.items()):
                if not file_path.startswith(path):
                    continue
                rest = file_path[len(path) :]
                if not rest or "/" in rest:
                    continue
                parts.append(
                    f"<D:response><D:href>{file_path}</D:href><D:propstat><D:prop>"
                    f"<D:getcontentlength>{len(content)}</D:getcontentlength>"
                    "<D:getlastmodified>Mon, 26 Oct 2026 10:00:00 GMT</D:getlastmodified>"
                    "</D:prop></D:propstat></D:response>"
                )
        parts.append("</D:multistatus>")
        return FakeResponse(207, "".join(parts).encode("utf-8"))


class FakeRequests:
    def __init__(self, dav: FakeDAV):
        self._dav = dav
        self.exceptions = SimpleNamespace(
            RequestException=FakeRequestException,
            ConnectionError=FakeRequestException,
            Timeout=FakeRequestException,
        )

    def request(self, method, url, **kwargs):
        return self._dav.handle(method, url, **kwargs)


class FakeServer:
    def __init__(self, base: str):
        self.base_dir = base
        self.settings = {
            "webdav_backup": {
                "enabled": True,
                "url": "https://dav.example.com",
                "username": "user",
                "password": "pass",
                "encryption_password": "secret",
            }
        }
        self.data_dir = os.path.join(base, "data", "web")
        os.makedirs(self.data_dir, exist_ok=True)
        self.logs: list[tuple[str, str]] = []

    def _save_data(self, name):
        return None

    def log_message(self, level, message, important=False):
        self.logs.append((level, message))


def _make_env(tmp_path, monkeypatch):
    dav = FakeDAV()
    monkeypatch.setattr(wd, "requests", FakeRequests(dav))
    monkeypatch.setattr(
        wd, "encrypt_bundle", lambda server, password, **kwargs: {"payload": "x"}
    )
    monkeypatch.setattr(wd, "decrypt_bundle", lambda raw, password: {"settings": {"ok": True}})
    monkeypatch.setattr(
        wd,
        "apply_bundle",
        lambda server, bundle, overwrite=True: {
            "imported": ["settings"],
            "skipped": [],
            "exported_at": "2026-10-26T00:00:00",
        },
    )
    server = FakeServer(str(tmp_path))
    return dav, server


@pytest.fixture
def env(tmp_path, monkeypatch):
    dav, server = _make_env(tmp_path, monkeypatch)
    return {"dav": dav, "server": server, "tmp_path": tmp_path}


def _revision_files(dav: FakeDAV) -> list[str]:
    return sorted(path for path in dav.files if "/revisions/" in path and path.endswith(".nbotcfg"))


def test_new_revision_id_format():
    rid = wd._new_revision_id()
    assert wd.REVISION_ID_PATTERN.match(rid)


def test_validate_revision_id_rejects_bad():
    for bad in ("", "2026-10-26", "../evil", "20261026-153000-ZZZZ"):
        with pytest.raises(wd.WebDAVBackupError):
            wd._validate_revision_id(bad)


def test_upload_backup_writes_latest_and_revision(env):
    dav, server = env["dav"], env["server"]
    result = wd.upload_backup(server, note="manual")

    assert "/nekobot/config.nbotcfg" in dav.files
    revision = result["revision"]
    assert revision["id"]
    assert revision["note"] == "manual"
    assert f"/nekobot/revisions/{revision['filename']}" in dav.files
    manifest = json.loads(dav.files["/nekobot/revisions/manifest.json"])
    assert [item["id"] for item in manifest["revisions"]] == [revision["id"]]
    assert server.settings["webdav_backup"]["last_revision_id"] == revision["id"]


def test_retention_prunes_old_revisions(env, monkeypatch):
    dav, server = env["dav"], env["server"]
    server.settings["webdav_backup"]["retention"] = {
        "enabled": True,
        "max_count": 2,
        "max_days": 365,
    }
    ids = iter(
        [
            "20260101-000001-aaaa",
            "20260101-000002-aaaa",
            "20260101-000003-aaaa",
        ]
    )
    monkeypatch.setattr(wd, "_new_revision_id", lambda now=None: next(ids))

    for index in range(3):
        server.settings["theme"] = str(index)
        wd.upload_backup(server)

    remaining = _revision_files(dav)
    assert len(remaining) == 2
    assert any("000003" in name for name in remaining)
    assert any("000002" in name for name in remaining)
    assert not any("000001" in name for name in remaining)
    manifest = json.loads(dav.files["/nekobot/revisions/manifest.json"])
    assert len(manifest["revisions"]) == 2


def test_retention_disabled_keeps_everything(env, monkeypatch):
    dav, server = env["dav"], env["server"]
    server.settings["webdav_backup"]["retention"] = {"enabled": False, "max_count": 1}
    ids = iter(["20260101-000001-aaaa", "20260101-000002-aaaa"])
    monkeypatch.setattr(wd, "_new_revision_id", lambda now=None: next(ids))

    wd.upload_backup(server)
    server.settings["theme"] = "light"
    wd.upload_backup(server)

    assert len(_revision_files(dav)) == 2


def test_list_revisions_uses_manifest(env):
    server = env["server"]
    first = wd.upload_backup(server)["revision"]["id"]
    server.settings["theme"] = "light"
    second = wd.upload_backup(server)["revision"]["id"]

    payload = wd.list_revisions(server)
    assert payload["source"] == "manifest"
    ids = [item["id"] for item in payload["revisions"]]
    assert set(ids) == {first, second}
    assert payload["retention"]["max_count"] == wd.DEFAULT_RETENTION["max_count"]


def test_unchanged_configuration_reuses_revision_despite_fresh_ciphertext(env, monkeypatch):
    dav, server = env["dav"], env["server"]
    ciphertexts = iter(["first-ciphertext", "second-ciphertext"])
    monkeypatch.setattr(
        wd, "encrypt_bundle", lambda server, password, **kwargs: {"payload": next(ciphertexts)}
    )
    first = wd.upload_backup(server)
    second = wd.upload_backup(server)
    assert first["deduplicated"] is False
    assert second["deduplicated"] is True
    assert first["revision"]["id"] == second["revision"]["id"]
    assert len(_revision_files(dav)) == 1


def test_configuration_change_creates_new_revision(env):
    dav, server = env["dav"], env["server"]
    first = wd.upload_backup(server)
    server.settings["theme"] = "light"
    second = wd.upload_backup(server)
    assert second["deduplicated"] is False
    assert first["revision"]["id"] != second["revision"]["id"]
    assert len(_revision_files(dav)) == 2


def test_missing_remote_revision_is_recreated(env):
    dav, server = env["dav"], env["server"]
    first = wd.upload_backup(server)
    del dav.files[f"/nekobot/revisions/{first['revision']['filename']}"]
    second = wd.upload_backup(server)
    assert second["deduplicated"] is False
    assert first["revision"]["id"] != second["revision"]["id"]


def test_changed_encryption_password_creates_new_revision(env):
    server = env["server"]
    first = wd.upload_backup(server)
    server.settings["webdav_backup"]["encryption_password"] = "new-key"
    second = wd.upload_backup(server)
    assert second["deduplicated"] is False
    assert first["revision"]["id"] != second["revision"]["id"]


def test_content_hash_ignores_backup_metadata_but_preserves_changes():
    original = {"exported_at": "first", "source": {"hostname": "a"}, "configs": {
        "settings": {"theme": "dark", "webdav_backup": {"url": "https://example.com"}},
    }}
    later = json.loads(json.dumps(original))
    later["exported_at"] = "later"
    later["source"]["hostname"] = "b"
    later["configs"]["settings"]["webdav_backup"].update(
        last_backup_at="later", last_revision_id="id", last_file_size=123,
    )
    assert wd._revision_content_hash(original) == wd._revision_content_hash(later)
    later["configs"]["settings"]["theme"] = "light"
    assert wd._revision_content_hash(original) != wd._revision_content_hash(later)


def test_list_revisions_falls_back_to_propfind(env):
    dav, server = env["dav"], env["server"]
    dav.folders.add("/nekobot/")
    dav.folders.add("/nekobot/revisions/")
    dav.files["/nekobot/revisions/20260101-000001-abcd.nbotcfg"] = b"{}"

    payload = wd.list_revisions(server)
    assert payload["source"] == "propfind"
    assert payload["revisions"][0]["id"] == "20260101-000001-abcd"


def test_download_revision_returns_bytes(env):
    server = env["server"]
    revision = wd.upload_backup(server)["revision"]
    payload = wd.download_revision(server, revision["id"])
    assert payload["filename"] == revision["filename"]
    assert json.loads(payload["content"].decode("utf-8")) == {"payload": "x"}


def test_delete_revision_removes_file_and_entry(env):
    dav, server = env["dav"], env["server"]
    revision = wd.upload_backup(server)["revision"]

    result = wd.delete_revision(server, revision["id"])
    assert result["removed"] is True
    assert f"/nekobot/revisions/{revision['filename']}" not in dav.files
    manifest = json.loads(dav.files["/nekobot/revisions/manifest.json"])
    assert manifest["revisions"] == []
    assert wd.list_revisions(server)["revisions"] == []


def test_delete_revision_missing_raises(env):
    with pytest.raises(wd.WebDAVBackupError):
        wd.delete_revision(env["server"], "20260101-000001-abcd")


def test_restore_revision_applies_bundle(env):
    server = env["server"]
    revision = wd.upload_backup(server)["revision"]

    result = wd.restore_revision(server, revision["id"])
    assert result["revision_id"] == revision["id"]
    assert result["imported"] == ["settings"]
    assert server.settings["webdav_backup"]["last_revision_id"] == revision["id"]
    assert server.settings["webdav_backup"]["last_sync_at"]


def test_restore_revision_without_password_set(env):
    server = env["server"]
    revision = wd.upload_backup(server)["revision"]
    server.settings["webdav_backup"]["encryption_password"] = ""
    with pytest.raises(wd.WebDAVBackupError):
        wd.restore_revision(server, revision["id"])


def test_assets_upload_and_download_tts(env, tmp_path):
    dav, server = env["dav"], env["server"]
    local_dir = os.path.join(server.data_dir, wd.TTS_CACHE_SUBDIR)
    os.makedirs(local_dir, exist_ok=True)
    with open(os.path.join(local_dir, "a.mp3"), "wb") as handle:
        handle.write(b"audio-bytes")
    with open(os.path.join(local_dir, "skip.exe"), "wb") as handle:
        handle.write(b"nope")

    uploaded = wd.sync_assets_upload(server, wd.TTS_CACHE_SUBDIR)
    assert uploaded["uploaded"] == 1
    assert "/nekobot/assets/tts_cache/a.mp3" in dav.files
    assert "/nekobot/assets/tts_cache/skip.exe" not in dav.files

    os.remove(os.path.join(local_dir, "a.mp3"))
    downloaded = wd.sync_assets_download(server, wd.TTS_CACHE_SUBDIR)
    assert downloaded["downloaded"] == 1
    with open(os.path.join(local_dir, "a.mp3"), "rb") as handle:
        assert handle.read() == b"audio-bytes"


def test_assets_upload_stickers(env, tmp_path, monkeypatch):
    sticker_dir = tmp_path / "stickers"
    sticker_dir.mkdir()
    (sticker_dir / "happy.png").write_bytes(b"png")
    monkeypatch.setattr(
        "nbot.services.sticker_library.get_sticker_dir", lambda: str(sticker_dir)
    )

    result = wd.sync_assets_upload(env["server"], wd.STICKERS_SUBDIR)
    assert result["uploaded"] == 1
    assert "/nekobot/assets/stickers/happy.png" in env["dav"].files


def test_selected_asset_kinds_respects_config(env):
    server = env["server"]
    settings = server.settings["webdav_backup"]
    assert wd._selected_asset_kinds(server, None, None) == []
    settings["sync_tts_cache"] = True
    assert wd._selected_asset_kinds(server, None, None) == [wd.TTS_CACHE_SUBDIR]
    assert wd._selected_asset_kinds(server, False, True) == [wd.STICKERS_SUBDIR]


def test_config_retention_normalization(env):
    from nbot.web.webdav_backup import _normalize_retention

    assert _normalize_retention(None) == wd.DEFAULT_RETENTION
    assert _normalize_retention({"max_count": 9999})["max_count"] == wd.MAX_RETENTION_COUNT
    assert _normalize_retention({"max_count": "bad"})["max_count"] == 20
    assert _normalize_retention({"max_days": 0})["max_days"] == 1


@pytest.fixture
def client(env):
    app = Flask(__name__)
    register_webdav_backup_routes(app, env["server"])
    app.config.update(TESTING=True)
    return app.test_client(), env


def test_route_config_exposes_retention_and_flags(client):
    http, _ = client
    response = http.get("/api/webdav/config")
    assert response.status_code == 200
    config = response.get_json()
    assert config["retention"]["enabled"] is True
    assert config["sync_tts_cache"] is False
    assert config["revisions_folder"] == "nekobot/revisions"


def test_route_save_config_updates_flags(client):
    http, env = client
    response = http.put(
        "/api/webdav/config",
        json={"retention": {"max_count": 2}, "sync_tts_cache": True},
    )
    assert response.status_code == 200
    config = response.get_json()["config"]
    assert config["retention"]["max_count"] == 2
    assert config["sync_tts_cache"] is True
    assert env["server"].settings["webdav_backup"]["sync_tts_cache"] is True


def test_route_backup_creates_revision(client):
    http, _ = client
    response = http.post("/api/webdav/backup", json={"note": "manual"})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["revision"]["id"]
    assert payload["retention"]["deleted"] == []


def test_route_revisions_list_download_restore_delete(client):
    http, _ = client
    revision_id = http.post("/api/webdav/backup", json={}).get_json()["revision"]["id"]

    listed = http.get("/api/webdav/revisions")
    assert listed.status_code == 200
    assert listed.get_json()["revisions"][0]["id"] == revision_id

    download = http.get(f"/api/webdav/revisions/{revision_id}/download")
    assert download.status_code == 200
    assert download.headers["Content-Type"] == "application/octet-stream"

    restored = http.post(f"/api/webdav/revisions/{revision_id}/restore", json={})
    assert restored.status_code == 200
    assert restored.get_json()["imported"] == ["settings"]

    deleted = http.delete(f"/api/webdav/revisions/{revision_id}")
    assert deleted.status_code == 200
    assert deleted.get_json()["remaining"] == 0


def test_route_sync_by_revision_id(client):
    http, _ = client
    revision_id = http.post("/api/webdav/backup", json={}).get_json()["revision"]["id"]
    response = http.post("/api/webdav/sync", json={"revision_id": revision_id})
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["imported"] == ["settings"]


def test_route_rejects_bad_revision_id(client):
    http, _ = client
    assert http.get("/api/webdav/revisions/bad-id/download").status_code == 400
    assert http.delete("/api/webdav/revisions/bad-id").status_code == 400
    assert http.post("/api/webdav/revisions/bad-id/restore", json={}).status_code == 400


def test_route_assets_sync(client, env):
    http = client[0]
    server = env["server"]
    os.makedirs(os.path.join(server.data_dir, wd.TTS_CACHE_SUBDIR), exist_ok=True)
    with open(os.path.join(server.data_dir, wd.TTS_CACHE_SUBDIR, "a.mp3"), "wb") as handle:
        handle.write(b"audio")

    response = http.post(
        "/api/webdav/assets/sync", json={"direction": "upload", "kinds": [wd.TTS_CACHE_SUBDIR]}
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["assets"][wd.TTS_CACHE_SUBDIR]["uploaded"] == 1


def test_route_assets_sync_validation(client):
    http, _ = client
    assert http.post("/api/webdav/assets/sync", json={"direction": "sideways"}).status_code == 400
    assert http.post("/api/webdav/assets/sync", json={}).status_code == 400
    assert (
        http.post("/api/webdav/assets/sync", json={"kinds": ["unknown"]}).status_code == 400
    )


def test_route_save_config_missing_fields_keep_existing(client):
    http, env = client
    server = env["server"]

    response = http.put(
        "/api/webdav/config",
        json={
            "enabled": True,
            "retention": {"max_count": 5},
            "sync_stickers": True,
            "folders": {"revisions": "ignored"},
        },
    )
    assert response.status_code == 200
    config = response.get_json()["config"]

    # 未传的字段保持原值（不会被清空），未知字段被忽略
    assert config["has_password"] is True
    assert config["has_encryption_password"] is True
    assert config["url"] == "https://dav.example.com"
    assert config["username"] == "user"
    assert config["retention"]["max_count"] == 5
    assert config["sync_stickers"] is True
    assert config["revisions_folder"] == "nekobot/revisions"
    raw = server.settings["webdav_backup"]
    assert raw["password"] == "pass"
    assert raw["encryption_password"] == "secret"


def test_route_save_config_clear_password_explicit(client):
    http, env = client
    response = http.put(
        "/api/webdav/config",
        json={"password": "", "clear_password": True},
    )
    assert response.status_code == 200
    assert response.get_json()["config"]["has_password"] is False
    assert env["server"].settings["webdav_backup"]["password"] == ""
