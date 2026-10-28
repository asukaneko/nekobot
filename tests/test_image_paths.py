"""结果路径脱敏 + provider 返回地址校验（#16 task_url / #17 结果图 URL）。"""

import json
import os
import re

import pytest
from flask import Flask

from nbot.services import image_service
from nbot.web.routes.images import register_image_routes

DRIVE_RE = re.compile(r"[A-Za-z]:[\\/]")


@pytest.fixture
def history_path(tmp_path, monkeypatch):
    path = tmp_path / "image_history.json"
    monkeypatch.setenv("NBOT_IMAGE_HISTORY_PATH", str(path))
    return path


def _fake_response(status_code=200, chunks=None):
    class FakeResponse:
        def __init__(self):
            self.status_code = status_code
            self._chunks = list(chunks or [])
            self.closed = False

        def iter_content(self, _size):
            yield from self._chunks

        def close(self):
            self.closed = True

    return FakeResponse()


def _public_dns(monkeypatch, ip="93.184.216.34"):
    import ipaddress

    def fake_getaddrinfo(host, port, proto=None, **kwargs):
        literal = str(host).strip("[]")
        try:
            ipaddress.ip_address(literal)
            resolved = literal
        except ValueError:
            resolved = ip
        return [(2, 1, 6, "", (resolved, port))]

    monkeypatch.setattr(image_service.socket, "getaddrinfo", fake_getaddrinfo)


# ---------------------------------------------------------------------------
# 路径脱敏
# ---------------------------------------------------------------------------


def test_static_relative_path_maps_and_rejects_outside():
    static_root = image_service.get_static_root()
    inside = os.path.join(static_root, "uploads", "image_lab", "a.png")
    assert image_service.static_relative_path(inside) == "uploads/image_lab/a.png"
    assert image_service.static_relative_path("uploads/image_lab/a.png") == (
        "uploads/image_lab/a.png"
    )
    assert image_service.static_relative_path("/etc/passwd") == ""
    assert image_service.static_relative_path("") == ""


def test_history_on_disk_has_no_absolute_path(monkeypatch, history_path):
    static_root = image_service.get_static_root()
    abs_path = os.path.join(static_root, "uploads", "image_lab", "lab_9.png")
    monkeypatch.setattr(
        image_service,
        "call_image_generation",
        lambda *a, **k: "data:image/png;base64,AAAA",
    )
    monkeypatch.setattr(
        image_service,
        "_save_generated_image",
        lambda data, prefix="": ("/static/uploads/image_lab/lab_9.png", abs_path),
    )
    result = image_service.generate_image("a cat", config=_config(), size="512x512")
    assert result["success"] is True

    raw = history_path.read_text(encoding="utf-8")
    assert not DRIVE_RE.search(raw)
    entry = json.loads(raw)["entries"][0]
    assert entry["results"][0]["path"] == "uploads/image_lab/lab_9.png"
    assert entry["results"][0]["name"] == "lab_9.png"
    # 返回体同样不吐本机路径
    assert result["images"][0]["path"] == "uploads/image_lab/lab_9.png"
    assert not DRIVE_RE.search(json.dumps(result, ensure_ascii=False))


def test_history_detail_api_returns_relative_path(history_path):
    image_service.append_image_history({
        "id": "e1",
        "created_at": "2026-10-27T10:00:00+08:00",
        "mode": "txt2img",
        "status": "success",
        "prompt": "cat",
        "results": [{
            "index": 0,
            "status": "success",
            "url": "/static/uploads/image_lab/lab_1.png",
            "path": "D:\\code\\repo\\nbot\\web\\static\\uploads\\image_lab\\lab_1.png",
            "size": "1024x1024",
            "elapsed_ms": 1,
            "error": "",
        }],
    })
    app = Flask(__name__)
    register_image_routes(
        app,
        type(
            "S",
            (),
            {"ai_models": [], "active_models_by_purpose": {}, "data_dir": ""},
        )(),
    )
    client = app.test_client()

    detail = client.get("/api/images/history/e1")
    body = detail.get_json()
    assert detail.status_code == 200
    path = body["entry"]["results"][0]["path"]
    assert not DRIVE_RE.search(json.dumps(body, ensure_ascii=False))
    # 静态根之外的历史路径一律置空，只保留 url/name
    assert path == ""
    assert body["entry"]["results"][0]["name"] == "lab_1.png"

    listed = client.get("/api/images/history").get_json()
    assert not DRIVE_RE.search(json.dumps(listed, ensure_ascii=False))


def test_delete_still_removes_file_with_relative_path(monkeypatch, tmp_path, history_path):
    static_root = tmp_path / "static"
    lab_dir = static_root / "uploads" / "image_lab"
    lab_dir.mkdir(parents=True)
    target = lab_dir / "lab_x.png"
    target.write_bytes(b"png")
    monkeypatch.setattr(image_service, "get_static_root", lambda: str(static_root))
    monkeypatch.setattr(image_service, "get_image_lab_upload_dir", lambda: str(lab_dir))

    image_service.append_image_history({
        "id": "e2",
        "created_at": "2026-10-27T10:00:00+08:00",
        "mode": "txt2img",
        "status": "success",
        "prompt": "cat",
        "results": [{
            "index": 0,
            "status": "success",
            "url": "/static/uploads/image_lab/lab_x.png",
            "path": "uploads/image_lab/lab_x.png",
            "size": "1024x1024",
            "elapsed_ms": 1,
            "error": "",
        }],
    })
    assert target.exists()
    assert image_service.delete_image_history_entry("e2", delete_files=True) is True
    assert not target.exists()


def test_delete_ignores_path_outside_lab_dir(monkeypatch, tmp_path, history_path):
    lab_dir = tmp_path / "image_lab"
    lab_dir.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"png")
    monkeypatch.setattr(image_service, "get_image_lab_upload_dir", lambda: str(lab_dir))
    image_service.append_image_history({
        "id": "e3",
        "created_at": "2026-10-27T10:00:00+08:00",
        "mode": "txt2img",
        "status": "success",
        "prompt": "cat",
        "results": [{"index": 0, "status": "success", "url": "", "path": str(outside)}],
    })
    image_service.delete_image_history_entry("e3", delete_files=True)
    assert outside.exists()


# ---------------------------------------------------------------------------
# #16 task_url 同源校验
# ---------------------------------------------------------------------------


def test_task_url_same_origin_accepted():
    base = "https://dashscope.aliyuncs.com/api/v1"
    task = "https://dashscope.aliyuncs.com/api/v1/tasks/abc"
    assert image_service._validated_task_url(task, base, "abc") == task


@pytest.mark.parametrize(
    "task_url",
    [
        "https://evil.example.com/steal",
        "http://dashscope.aliyuncs.com/api/v1/tasks/abc",  # scheme 不同
        "https://dashscope.aliyuncs.com:8443/api/v1/tasks/abc",  # 端口不同
        "https://dashscope.aliyuncs.com.evil.example.com/tasks/abc",
        "file:///etc/passwd",
    ],
)
def test_task_url_cross_origin_falls_back(task_url):
    base = "https://dashscope.aliyuncs.com/api/v1"
    assert image_service._validated_task_url(task_url, base, "abc") == (
        "https://dashscope.aliyuncs.com/api/v1/tasks/abc"
    )


def test_task_url_missing_uses_fallback():
    base = "https://dashscope.aliyuncs.com/api/v1"
    assert image_service._validated_task_url("", base, "xyz") == (
        "https://dashscope.aliyuncs.com/api/v1/tasks/xyz"
    )


# ---------------------------------------------------------------------------
# #17 结果图 URL 抓取校验
# ---------------------------------------------------------------------------


def test_download_generated_image_rejects_private(monkeypatch):
    _public_dns(monkeypatch)
    assert image_service._download_generated_image("http://127.0.0.1/x.png") is None
    assert image_service._download_generated_image("http://192.168.1.5/x.png") is None


def test_download_generated_image_rejects_redirect(monkeypatch):
    _public_dns(monkeypatch)
    fake = _fake_response(status_code=302, chunks=[b"x"])
    monkeypatch.setattr(image_service.requests, "get", lambda *a, **k: fake)
    assert image_service._download_generated_image("https://cdn.example.com/x.png") is None
    assert fake.closed is True


def test_download_generated_image_ok(monkeypatch):
    _public_dns(monkeypatch)
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _fake_response(status_code=200, chunks=[b"ab", b"cd"])

    monkeypatch.setattr(image_service.requests, "get", fake_get)
    assert image_service._download_generated_image("https://cdn.example.com/x.png") == b"abcd"
    assert captured["allow_redirects"] is False
    assert captured["stream"] is True


def test_save_image_to_uploads_blocks_private_url(monkeypatch, tmp_path):
    _public_dns(monkeypatch)
    upload_dir = tmp_path / "uploads"
    result = image_service.save_image_to_uploads("http://127.0.0.1/x.png", str(upload_dir))
    assert result is None
    assert not upload_dir.exists() or not any(upload_dir.iterdir())


def _config():
    return {
        "api_key": "test-key",
        "base_url": "https://api.example.test/v1/images/generations",
        "proxy_url": "",
        "model": "test-image-model",
        "provider": "openai",
        "provider_type": "openai_compatible",
        "append_base_url_path": True,
        "size": "",
    }
