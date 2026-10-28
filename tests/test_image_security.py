"""生图参考图安全：本地越权读取 / SSRF / 体积上限 / nosniff 测试。"""

import base64
import ipaddress
import os
import types

import pytest
from flask import Flask

from nbot.services import image_service
from nbot.web.routes.images import register_image_routes


@pytest.fixture
def reference_dir(tmp_path, monkeypatch):
    ref_dir = tmp_path / "references"
    ref_dir.mkdir()
    monkeypatch.setattr(image_service, "get_image_reference_dir", lambda: str(ref_dir))
    return ref_dir


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
    """模拟 DNS：IP 字面量原样返回，域名解析到公网 IP。"""

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
# 本地路径：只允许参考图库内文件
# ---------------------------------------------------------------------------


def test_resolve_reference_path_accepts_file_in_library(reference_dir):
    target = reference_dir / "a.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n")
    resolved = image_service.resolve_reference_path(
        "/static/uploads/image_lab/references/a.png"
    )
    assert os.path.realpath(resolved) == os.path.realpath(target)
    assert image_service.resolve_reference_path("a.png") == os.path.realpath(target)


@pytest.mark.parametrize(
    "source",
    [
        "/etc/passwd",
        "C:\\Windows\\win.ini",
        "~/secret.png",
        "/static/../data/web/api_keys.json",
        "/static/uploads/image_lab/references/../../../../data/web/api_keys.json",
        "/static/uploads/character_images/x.png",
        "/static/uploads/image_lab/other/x.png",
        "sub/dir.png",
        "../a.png",
        "..\\a.png",
    ],
)
def test_resolve_reference_path_rejects_traversal(reference_dir, source):
    with pytest.raises(ValueError):
        image_service.resolve_reference_path(source)


def test_load_reference_bytes_rejects_outside_file(reference_dir, tmp_path):
    outside = tmp_path / "api_keys.json"
    outside.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        image_service.load_reference_bytes(str(outside))
    # 通过 /static/ 前缀绕出去也不行
    with pytest.raises(ValueError):
        image_service.load_reference_bytes("/static/../data/web/api_keys.json")


def test_load_reference_bytes_rejects_symlink(reference_dir, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret-bytes")
    link = reference_dir / "link.png"
    try:
        os.symlink(secret, link)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建软链")
    with pytest.raises(ValueError):
        image_service.load_reference_bytes("link.png")


def test_load_reference_bytes_reads_library_file(reference_dir):
    target = reference_dir / "b.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n-demo")
    assert image_service.load_reference_bytes("b.png") == b"\x89PNG\r\n\x1a\n-demo"


# ---------------------------------------------------------------------------
# URL：只允许公网 http(s)，禁重定向
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/a.png",
        "http://localhost/a.png",
        "http://[::1]/a.png",
        "http://169.254.169.254/latest/meta-data",
        "http://10.1.2.3/a.png",
        "http://192.168.1.10/a.png",
        "file:///etc/passwd",
        "ftp://example.com/a.png",
        "http://host.local/a.png",
    ],
)
def test_assert_public_http_url_rejects_private(monkeypatch, url):
    _public_dns(monkeypatch)
    with pytest.raises(ValueError):
        image_service.assert_public_http_url(url)


def test_assert_public_http_url_rejects_private_dns(monkeypatch):
    _public_dns(monkeypatch, "10.0.0.7")
    with pytest.raises(ValueError):
        image_service.assert_public_http_url("https://evil.example.com/a.png")


def test_assert_public_http_url_accepts_public(monkeypatch):
    _public_dns(monkeypatch)
    image_service.assert_public_http_url("https://example.com/a.png")


def test_assert_public_http_url_rejects_unresolvable(monkeypatch):
    def fail(*args, **kwargs):
        raise image_service.socket.gaierror("nope")

    monkeypatch.setattr(image_service.socket, "getaddrinfo", fail)
    with pytest.raises(ValueError):
        image_service.assert_public_http_url("https://nope.invalid/a.png")


def test_download_reference_rejects_redirect(monkeypatch):
    _public_dns(monkeypatch)
    fake = _fake_response(status_code=302, chunks=[b"x"])
    monkeypatch.setattr(image_service.requests, "get", lambda *a, **k: fake)
    with pytest.raises(ValueError, match="重定向"):
        image_service.load_reference_bytes("https://example.com/a.png")
    assert fake.closed is True


def test_download_reference_enforces_size_cap_streaming(monkeypatch):
    _public_dns(monkeypatch)
    monkeypatch.setattr(
        image_service,
        "get_reference_limits",
        lambda: {
            "max_reference_images": 4,
            "max_reference_bytes": 10,
            "max_reference_total_bytes": 20,
        },
    )
    fake = _fake_response(status_code=200, chunks=[b"a" * 8, b"b" * 8])
    monkeypatch.setattr(image_service.requests, "get", lambda *a, **k: fake)
    with pytest.raises(ValueError, match="过大"):
        image_service.load_reference_bytes("https://example.com/a.png")


def test_download_reference_returns_bytes(monkeypatch):
    _public_dns(monkeypatch)
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _fake_response(status_code=200, chunks=[b"abc", b"def"])

    monkeypatch.setattr(image_service.requests, "get", fake_get)
    assert image_service.load_reference_bytes("https://example.com/a.png") == b"abcdef"
    assert captured["allow_redirects"] is False
    assert captured["stream"] is True


# ---------------------------------------------------------------------------
# 体积上限：单图 + 合计
# ---------------------------------------------------------------------------


def test_normalize_reference_images_rejects_total_over_cap(monkeypatch, reference_dir):
    monkeypatch.setattr(
        image_service,
        "get_reference_limits",
        lambda: {
            "max_reference_images": 4,
            "max_reference_bytes": 100,
            "max_reference_total_bytes": 12,
        },
    )
    for name in ("a.png", "b.png", "c.png"):
        (reference_dir / name).write_bytes(b"x" * 8)
    with pytest.raises(ValueError, match="合计过大"):
        image_service.normalize_reference_images(["a.png", "b.png"])
    # 单张时（8 字节 < 12）通过
    assert image_service.normalize_reference_images(["a.png"]) == ["a.png"]


def test_normalize_reference_images_uses_settings_count(monkeypatch, reference_dir):
    monkeypatch.setattr(
        image_service,
        "get_reference_limits",
        lambda: {
            "max_reference_images": 1,
            "max_reference_bytes": 100,
            "max_reference_total_bytes": 200,
        },
    )
    (reference_dir / "a.png").write_bytes(b"x")
    (reference_dir / "b.png").write_bytes(b"y")
    with pytest.raises(ValueError, match="最多 1 张"):
        image_service.normalize_reference_images(["a.png", "b.png"])


def test_data_uri_cap_uses_settings(monkeypatch):
    monkeypatch.setattr(
        image_service,
        "get_reference_limits",
        lambda: {
            "max_reference_images": 4,
            "max_reference_bytes": 4,
            "max_reference_total_bytes": 8,
        },
    )
    payload = base64.b64encode(b"12345").decode()
    with pytest.raises(ValueError, match="过大"):
        image_service.load_reference_bytes(f"data:image/png;base64,{payload}")


# ---------------------------------------------------------------------------
# 路由：上传上限对齐 + nosniff
# ---------------------------------------------------------------------------


def _client():
    app = Flask(__name__)
    server = types.SimpleNamespace(ai_models=[], active_models_by_purpose={}, data_dir="")
    register_image_routes(app, server)
    app.config["TESTING"] = True
    return app.test_client()


def test_upload_route_uses_aligned_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(image_service, "get_image_reference_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        image_service,
        "get_reference_limits",
        lambda: {
            "max_reference_images": 4,
            "max_reference_bytes": 8,
            "max_reference_total_bytes": 16,
        },
    )
    client = _client()
    data = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    response = client.post(
        "/api/images/references",
        data={"file": (__import__("io").BytesIO(data), "ref.png")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert "过大" in response.get_json()["error"]


def test_static_image_response_has_nosniff():
    client = _client()
    response = client.get("/static/uploads/image_lab/missing.png")
    assert response.headers.get("X-Content-Type-Options") == "nosniff"


def test_styles_route_exposes_total_limit(monkeypatch):
    monkeypatch.setattr(
        image_service,
        "get_reference_limits",
        lambda: {
            "max_reference_images": 3,
            "max_reference_bytes": 1024,
            "max_reference_total_bytes": 2048,
        },
    )
    client = _client()
    body = client.get("/api/images/styles").get_json()
    assert body["limits"]["max_reference_total_bytes"] == 2048
    assert body["limits"]["max_reference_images"] == 3
