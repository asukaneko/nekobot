"""配置包容器兼容性测试（issue #16）。

覆盖：JSON（含 BOM / GB18030）、ZIP、GZIP、裸二进制「salt + Fernet token」，
以及 salt 以原始字节内嵌在 JSON 字符串中的伪 JSON 配置包。所有路径都必须
走二进制读取并按文件头嗅探，不允许出现 UnicodeDecodeError。
"""

import base64
import gzip
import io
import json
import os
import zipfile

import pytest
from flask import Flask

from nbot.web import config_transfer as ct
from nbot.web import webdav_backup as wd
from nbot.web.routes.config_transfer import register_config_transfer_routes

PASSWORD = "secret"
# 复现 issue #16：salt 前两个字节就是报错里的 0xba / 0xd7
BINARY_SALT = bytes([0xBA, 0xD7] * (ct.KDF_SALT_BYTES // 2))


def _plain_bundle(configs=None):
    return {
        "version": ct.BUNDLE_VERSION,
        "type": "nbot_config_bundle",
        "exported_at": "2026-11-21T10:00:00",
        "configs": configs if configs is not None else {"settings": {"theme": "dark"}},
    }


def _salt_and_token(password=PASSWORD, configs=None, salt=None):
    salt = salt or os.urandom(ct.KDF_SALT_BYTES)
    plaintext = json.dumps(_plain_bundle(configs), ensure_ascii=False).encode("utf-8")
    token = ct._fernet_from_password(password, salt).encrypt(plaintext)
    return salt, token


def _json_bytes(password=PASSWORD, configs=None):
    salt, token = _salt_and_token(password, configs)
    bundle = {
        "version": ct.BUNDLE_VERSION,
        "type": "nbot_config_bundle",
        "encrypted": True,
        "algorithm": "fernet",
        "kdf": "pbkdf2_hmac_sha256",
        "iterations": ct.KDF_ITERATIONS,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "exported_at": "2026-11-21T10:00:00",
        "payload": token.decode("ascii"),
    }
    return json.dumps(bundle, ensure_ascii=False).encode("utf-8")


def _zip_bytes(member_bytes, member_name="config.nbotcfg"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, member_bytes)
        archive.writestr("manifest.json", json.dumps({"version": 2}))
    return buffer.getvalue()


def _pseudo_json_bytes(password=PASSWORD):
    """salt 直接写成原始二进制字节的伪 JSON（issue #16 报告的文件形态）。

    字段顺序按报告线索排列：``{"salt":"`` 正好 10 字节，第 11 个字节就是
    salt 的第一个字节（报告中的 0xba / 0xd7），因此每个备份都随机不同。
    """
    salt, token = _salt_and_token(password, salt=BINARY_SALT)
    return (
        b'{"salt": "' + salt + b'", "payload": "' + token + b'",'
        b'"version":1,"type":"nbot_config_bundle","encrypted":true,'
        b'"algorithm":"fernet"}'
    )


class FakeServer:
    def __init__(self, base_dir):
        self.base_dir = str(base_dir)
        self.data_dir = os.path.join(self.base_dir, "data", "web")
        os.makedirs(self.data_dir, exist_ok=True)
        self.settings = {}
        self.sessions = {}
        self.ai_config = {}
        self.ai_models = []
        self.active_model_id = None
        self.active_models_by_purpose = {}
        self.skills_config = []
        self.tools_config = []
        self.channels_config = []
        self.heartbeat_config = {}
        self.scheduled_tasks = []
        self.workflows = []
        self.memories = []
        self.custom_personality_presets = []
        self.personality = {}
        self.saved = []
        self.logs = []

    def _save_data(self, name):
        self.saved.append(name)

    def log_message(self, level, message, important=False):
        self.logs.append((level, message))


# ============================================================
# 容器嗅探
# ============================================================


def test_sniff_bundle_format():
    assert ct.sniff_bundle_format(b"") == "empty"
    assert ct.sniff_bundle_format(_zip_bytes(_json_bytes())) == "zip"
    assert ct.sniff_bundle_format(gzip.compress(_json_bytes())) == "gzip"
    assert ct.sniff_bundle_format(_json_bytes()) == "json"
    assert ct.sniff_bundle_format(b"\xef\xbb\xbf" + _json_bytes()) == "json"
    assert ct.sniff_bundle_format(_pseudo_json_bytes()) == "json"
    assert ct.sniff_bundle_format(b"\xba\xd7raw-binary-payload") == "binary"


# ============================================================
# 各容器格式都能加载
# ============================================================


def test_load_plain_json_bundle():
    bundle = ct.load_bundle_bytes(_json_bytes(), PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"


def test_load_json_bundle_with_utf8_bom():
    raw = b"\xef\xbb\xbf" + _json_bytes()
    assert ct.load_bundle_bytes(raw, PASSWORD)["configs"]["settings"]["theme"] == "dark"


def test_load_gb18030_json_bundle():
    salt, token = _salt_and_token()
    payload = {
        "version": ct.BUNDLE_VERSION,
        "type": "nbot_config_bundle",
        "encrypted": True,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "payload": token.decode("ascii"),
        # 中文内容 + GB18030 编码（Windows 端导出的常见形态）
        "exported_at": "2026-11-21T10:00:00",
        "source": {"hostname": "测试主机"},
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("gb18030")
    assert ct.load_bundle_bytes(raw, PASSWORD)["configs"]["settings"]["theme"] == "dark"


def test_load_zip_bundle():
    raw = _zip_bytes(_json_bytes())
    assert ct.load_bundle_bytes(raw, PASSWORD)["configs"]["settings"]["theme"] == "dark"


def test_load_gzip_bundle():
    raw = gzip.compress(_json_bytes())
    assert ct.load_bundle_bytes(raw, PASSWORD)["configs"]["settings"]["theme"] == "dark"


def test_load_gzip_wrapped_zip_bundle():
    raw = gzip.compress(_zip_bytes(_json_bytes()))
    assert ct.load_bundle_bytes(raw, PASSWORD)["configs"]["settings"]["theme"] == "dark"


# ============================================================
# 裸二进制 / 伪 JSON（issue #16 的根因）
# ============================================================


def test_load_binary_salt_prefix_with_raw_token():
    salt, token = _salt_and_token(salt=BINARY_SALT)
    raw = salt + base64.urlsafe_b64decode(token)
    bundle = ct.load_bundle_bytes(raw, PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"


def test_load_binary_salt_prefix_with_base64_token():
    salt, token = _salt_and_token(salt=BINARY_SALT)
    bundle = ct.load_bundle_bytes(salt + token, PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"


def test_load_pseudo_json_with_binary_salt():
    """issue #16 的原始报错场景：0xba 出现在第 11 个字节，旧代码直接崩。"""
    raw = _pseudo_json_bytes()
    assert raw[10] == 0xBA
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")

    bundle = ct.load_bundle_bytes(raw, PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"


def test_load_pseudo_json_with_whitespace_and_long_salt():
    """字段间有空白、salt 后额外补齐时仍能定位。"""
    salt, token = _salt_and_token(salt=BINARY_SALT)
    raw = (
        b'{\n  "salt" :  "' + salt + b'",\n  "payload" : "' + token + b'"\n}'
    )
    assert ct.load_bundle_bytes(raw, PASSWORD)["configs"]["settings"]["theme"] == "dark"


# ============================================================
# 错误路径：报错信息可用，且不再是 UnicodeDecodeError
# ============================================================


@pytest.mark.parametrize(
    "raw_factory",
    [
        lambda: _json_bytes(),
        lambda: _zip_bytes(_json_bytes()),
        lambda: gzip.compress(_json_bytes()),
        lambda: _pseudo_json_bytes(),
        lambda: _salt_and_token()[0] + _salt_and_token()[1],
    ],
)
def test_wrong_password_raises_config_transfer_error(raw_factory):
    with pytest.raises(ct.ConfigTransferError) as excinfo:
        ct.load_bundle_bytes(raw_factory(), "wrong-password")
    assert not isinstance(excinfo.value, UnicodeDecodeError)
    assert "密码" in str(excinfo.value)


def test_unknown_bytes_report_format_and_preview():
    raw = bytes([0xBA, 0xD7]) + os.urandom(64)
    with pytest.raises(ct.ConfigTransferError) as excinfo:
        ct.load_bundle_bytes(raw, PASSWORD)
    message = str(excinfo.value)
    assert "无法识别" in message
    assert "头16字节=ba d7" in message


def test_empty_bundle_rejected():
    with pytest.raises(ct.ConfigTransferError, match="为空"):
        ct.load_bundle_bytes(b"", PASSWORD)


def test_zip_without_config_member_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("portrait.png", b"png")
    with pytest.raises(ct.ConfigTransferError, match="未找到配置文件"):
        ct.load_bundle_bytes(buffer.getvalue(), PASSWORD)


def test_decrypt_bundle_tolerates_missing_base64_padding():
    salt, token = _salt_and_token(salt=BINARY_SALT)
    bundle = {
        "type": "nbot_config_bundle",
        "encrypted": True,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        "payload": token.decode("ascii").rstrip("="),
    }
    assert ct.decrypt_bundle(bundle, PASSWORD)["configs"]["settings"]["theme"] == "dark"


# ============================================================
# 调用方：ZIP 解包 / HTTP 路由 / WebDAV 同步与版本恢复
# ============================================================


def test_extract_zip_bundle_handles_binary_member():
    salt, token = _salt_and_token(salt=BINARY_SALT)
    raw = _zip_bytes(salt + token)
    bundle, portraits = ct.extract_zip_bundle(raw, PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"
    assert portraits == {}


def test_extract_zip_bundle_handles_pseudo_json_member():
    bundle, _ = ct.extract_zip_bundle(_zip_bytes(_pseudo_json_bytes()), PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"


@pytest.fixture
def import_client(tmp_path):
    server = FakeServer(tmp_path)
    app = Flask(__name__)
    register_config_transfer_routes(app, server)
    app.config.update(TESTING=True)
    return app.test_client(), server


def _upload(client, data, filename):
    return client.post(
        "/api/config-transfer/import",
        data={"file": (io.BytesIO(data), filename), "password": PASSWORD},
        content_type="multipart/form-data",
    )


def test_route_imports_binary_nbotcfg(import_client):
    http, server = import_client
    salt, token = _salt_and_token(salt=BINARY_SALT)
    response = _upload(http, salt + token, "config.nbotcfg")

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert server.settings == {"theme": "dark"}
    assert "settings" in server.saved


def test_route_imports_pseudo_json_nbotcfg(import_client):
    http, server = import_client
    response = _upload(http, _pseudo_json_bytes(), "config.nbotcfg")

    assert response.status_code == 200
    assert server.settings == {"theme": "dark"}


def test_route_detects_zip_by_content_not_extension(import_client):
    http, _server = import_client
    response = _upload(http, _zip_bytes(_json_bytes()), "config.nbotcfg")
    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_route_imports_zip_named_without_zip_extension(import_client):
    http, server = import_client
    response = _upload(http, _zip_bytes(_pseudo_json_bytes()), "backup.bin")
    assert response.status_code == 200
    assert server.settings == {"theme": "dark"}


def test_route_reports_clean_error_for_garbage(import_client):
    http, _server = import_client
    response = _upload(http, os.urandom(96), "config.nbotcfg")
    body = response.get_json()
    assert response.status_code == 400
    assert "无法识别" in body["error"]
    assert "UnicodeDecodeError" not in body["error"]


class _FakeResponse:
    def __init__(self, status_code, content=b"", headers=None):
        self.status_code = status_code
        self.content = content
        self.text = content.decode("utf-8", "replace")
        self.headers = headers or {}


class _FakeRequests:
    """只服务 GET/HEAD：把 payload 当作远程 config.nbotcfg 内容返回。"""

    class exceptions:  # noqa: N801 - 模拟 requests.exceptions
        RequestException = Exception
        ConnectionError = OSError
        Timeout = TimeoutError

    def __init__(self, payload):
        self.payload = payload

    def request(self, method, url, **kwargs):
        if method == "GET":
            return _FakeResponse(200, self.payload)
        if method == "HEAD":
            return _FakeResponse(200, headers={"Content-Length": str(len(self.payload))})
        if method == "PROPFIND":
            return _FakeResponse(404)
        return _FakeResponse(200)


def _webdav_server(tmp_path, encryption_password=PASSWORD):
    server = FakeServer(tmp_path)
    server.settings = {
        "webdav_backup": {
            "enabled": True,
            "url": "https://dav.example.com",
            "encryption_password": encryption_password,
        }
    }
    return server


def test_pull_backup_accepts_binary_payload(tmp_path, monkeypatch):
    salt, token = _salt_and_token(salt=BINARY_SALT)
    monkeypatch.setattr(wd, "requests", _FakeRequests(salt + token))
    server = _webdav_server(tmp_path)

    result = wd.pull_backup(server)

    assert result["ok"] is True
    assert result["imported"] == ["settings"]
    assert server.settings["theme"] == "dark"
    assert server.settings["webdav_backup"]["last_error"] == ""


def test_pull_backup_accepts_pseudo_json_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "requests", _FakeRequests(_pseudo_json_bytes()))
    server = _webdav_server(tmp_path)

    result = wd.pull_backup(server)
    assert result["ok"] is True
    assert server.settings["theme"] == "dark"


def test_pull_backup_reports_format_error_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "requests", _FakeRequests(bytes([0xBA, 0xD7]) + os.urandom(80)))
    server = _webdav_server(tmp_path)

    with pytest.raises(wd.WebDAVBackupError) as excinfo:
        wd.pull_backup(server)
    assert "无法识别" in str(excinfo.value)
    assert "无法识别" in server.settings["webdav_backup"]["last_error"]


def test_restore_revision_accepts_binary_payload(tmp_path, monkeypatch):
    salt, token = _salt_and_token(salt=BINARY_SALT)
    monkeypatch.setattr(wd, "requests", _FakeRequests(salt + token))
    server = _webdav_server(tmp_path)

    result = wd.restore_revision(server, "20261121-100000-ab12")
    assert result["ok"] is True
    assert server.settings["theme"] == "dark"
    assert server.settings["webdav_backup"]["last_revision_id"] == "20261121-100000-ab12"


def test_restore_revision_error_is_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "requests", _FakeRequests(_json_bytes()))
    server = _webdav_server(tmp_path, encryption_password="wrong")

    with pytest.raises(wd.WebDAVBackupError) as excinfo:
        wd.restore_revision(server, "20261121-100000-ab12")
    assert "密码" in str(excinfo.value)


def test_pull_backup_unaffected_for_standard_json_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(wd, "requests", _FakeRequests(_json_bytes()))
    server = _webdav_server(tmp_path)

    result = wd.pull_backup(server)
    assert result["ok"] is True
    assert result["imported"] == ["settings"]
    assert server.settings["theme"] == "dark"


# ============================================================
# Review 回归：容器/编码边界条件
# ============================================================


def _portrait_zip_bytes(member_bytes, member_name="config.nbotcfg", portrait=b"png"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(member_name, member_bytes)
        archive.writestr("portraits/alice.png", portrait)
        archive.writestr(
            "manifest.json",
            json.dumps({"version": 2, "portraits": ["portraits/alice.png"]}),
        )
    return buffer.getvalue()


def test_truncated_gzip_raises_config_transfer_error():
    """Codex/CodeRabbit P2：截断的 GZIP 抛 EOFError，不能逃逸成 500。"""
    raw = gzip.compress(_json_bytes())[:20]
    with pytest.raises(ct.ConfigTransferError) as excinfo:
        ct.load_bundle_bytes(raw, PASSWORD)
    assert "GZIP" in str(excinfo.value)
    assert "截断" in str(excinfo.value)


def test_corrupt_gzip_payload_raises_config_transfer_error():
    blob = bytearray(gzip.compress(_json_bytes()))
    blob[-5] ^= 0xFF  # 破坏 CRC
    with pytest.raises(ct.ConfigTransferError) as excinfo:
        ct.load_bundle_bytes(bytes(blob), PASSWORD)
    assert "GZIP" in str(excinfo.value)


def test_out_of_range_integer_salt_is_rejected_not_raised():
    """Codex/CodeRabbit P2：salt 写成越界整数数组时不能抛 ValueError。"""
    raw = json.dumps(
        {
            "type": "nbot_config_bundle",
            "encrypted": True,
            "salt": [256, 300],
            "payload": "A" * 64,
        }
    ).encode("utf-8")
    with pytest.raises(ct.ConfigTransferError) as excinfo:
        ct.load_bundle_bytes(raw, PASSWORD)
    assert excinfo.type is ct.ConfigTransferError
    assert "无法识别" in str(excinfo.value)


def test_valid_integer_salt_array_still_works():
    salt, token = _salt_and_token(salt=BINARY_SALT)
    raw = json.dumps(
        {
            "type": "nbot_config_bundle",
            "encrypted": True,
            "salt": list(salt),
            "payload": token.decode("ascii"),
        }
    ).encode("utf-8")
    assert ct.load_bundle_bytes(raw, PASSWORD)["configs"]["settings"]["theme"] == "dark"


def test_pseudo_json_with_printable_ascii_salt_is_recovered():
    """CodeRabbit minor：salt 恰好全是可打印 ASCII 时，文本层解析成功后仍要回退。"""
    salt, token = _salt_and_token(salt=b"abcdefghijklmnop")
    raw = (
        b'{"salt": "' + salt + b'", "payload": "' + token + b'",'
        b'"type":"nbot_config_bundle","encrypted":true}'
    )
    # 文本层可以完整解析，但用手里的 salt 解密必然失败
    assert json.loads(raw.decode("utf-8"))["salt"] == "abcdefghijklmnop"
    bundle = ct.load_bundle_bytes(raw, PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"


def test_binary_bundle_with_gzip_magic_salt_falls_back():
    """CodeRabbit minor：salt 以 \\x1f\\x8b 开头时不能被 GZIP 分支吞掉。"""
    salt = b"\x1f\x8b" + bytes(range(14))
    _salt, token = _salt_and_token(salt=salt)
    raw = salt + token
    assert ct.sniff_bundle_format(raw) == "gzip"

    bundle = ct.load_bundle_bytes(raw, PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"


@pytest.mark.parametrize("wrapper", ["zip", "gzip+zip"])
def test_portraits_survive_nested_containers(wrapper):
    """CodeRabbit minor：GZIP 包裹 ZIP 时立绘不能被丢掉。"""
    raw = _portrait_zip_bytes(_json_bytes())
    if wrapper == "gzip+zip":
        raw = gzip.compress(raw)

    bundle, portraits = ct.load_bundle_bytes_with_portraits(raw, PASSWORD)
    assert bundle["configs"]["settings"]["theme"] == "dark"
    assert portraits == {"portraits/alice.png": b"png"}


def test_route_restores_portraits_from_gzip_wrapped_zip(import_client):
    http, server = import_client
    configs = {"settings": {"theme": "dark"}, "custom_personality_presets": []}
    payload = _portrait_zip_bytes(_json_bytes(configs=configs))
    response = _upload(http, gzip.compress(payload), "backup.nbotcfg")

    body = response.get_json()
    assert response.status_code == 200
    assert body["success"] is True
    assert body["portraits_restored"] == 1

    upload_dir = os.path.join(server.base_dir, "nbot", "web", "static", "uploads", "portraits")
    assert os.listdir(upload_dir) and os.listdir(upload_dir)[0].endswith(".png")

