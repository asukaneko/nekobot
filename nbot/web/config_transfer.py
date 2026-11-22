import base64
import binascii
import codecs
import gzip
import io
import json
import logging
import os
import platform
import re
import zipfile
import zlib
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Iterable, Tuple

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from nbot.web.secure_store import read_secure_json, write_secure_json

_log = logging.getLogger(__name__)


BUNDLE_VERSION = 1
KDF_ITERATIONS = 390000
KDF_SALT_BYTES = 16
CONFIG_KEYS = (
    "settings",
    "sessions",
    "ai_config",
    "ai_models",
    "api_keys",
    "active_model_id",
    "active_models_by_purpose",
    "skills",
    "tools",
    "channels",
    "heartbeat",
    "scheduled_tasks",
    "workflows",
    "memories",
    "personality",
    "custom_personality_presets",
    "world_books",
    "mcp_servers",
    "memory_fs",
)
SAVE_TYPES = (
    "settings",
    "sessions",
    "ai_config",
    "ai_models",
    "skills",
    "tools",
    "channels",
    "heartbeat",
    "scheduled_tasks",
    "workflows",
    "memories",
    "custom_personality_presets",
)


# 配置包容器魔数：按文件头嗅探，不依赖扩展名
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_GZIP_MAGIC = b"\x1f\x8b"
# 文本配置包可能出现的编码（Windows 端导出可能落在 GBK/GB18030）
_BUNDLE_TEXT_ENCODINGS = ("utf-8-sig", "gb18030")
# 嵌套容器（gzip 套 zip、zip 套 zip）的最大解包层数
_BUNDLE_MAX_DEPTH = 3
# 单层解包后的体积上限，防御压缩炸弹
_BUNDLE_MAX_BYTES = 512 * 1024 * 1024
# 一次导入中累计收集的立绘上限（跨嵌套层共享），防止多成员叠加吃满内存
_PORTRAITS_MAX_BYTES = 128 * 1024 * 1024
_PORTRAITS_MAX_FILES = 500
# Fernet token 由 Base64 字母表组成，用于从伪 JSON 中截取 payload
_B64_CHARS = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_+=")
# 伪 JSON 中 payload 为原始 token 字节时，最多尝试多少个「引号位置」作为结尾
_PSEUDO_JSON_TOKEN_TRIES = 16


class ConfigTransferError(ValueError):
    pass


class _BundleFormatError(ConfigTransferError):
    """当前容器层级无法识别（用于在同一文件上继续尝试下一种布局）。"""


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))


def _fernet_from_password(password: str, salt: bytes) -> Fernet:
    if not password or not password.strip():
        raise ConfigTransferError("导出/导入密码不能为空")
    return Fernet(_derive_key(password.strip(), salt))


def _clean_json(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _read_personality(server) -> Dict[str, Any]:
    path = os.path.join(server.base_dir, "resources", "prompts", "personality.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return deepcopy(getattr(server, "personality", {}) or {})


def _read_api_keys(server) -> Any:
    path = os.path.join(server.data_dir, "api_keys.json")
    try:
        data, _ = read_secure_json(path, server.data_dir, [])
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_api_keys(server, api_keys: Any) -> None:
    path = os.path.join(server.data_dir, "api_keys.json")
    write_secure_json(path, server.data_dir, api_keys if isinstance(api_keys, list) else [])


def _write_personality(server, personality: Dict[str, Any]) -> None:
    path = os.path.join(server.base_dir, "resources", "prompts", "personality.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(personality or {}, f, ensure_ascii=False, indent=2)
    server.personality = personality or {}


def _read_world_books(server) -> Dict[str, Any]:
    path = os.path.join(server.base_dir, "data", "world_books.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_world_books(server, world_books: Dict[str, Any]) -> None:
    path = os.path.join(server.base_dir, "data", "world_books.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(world_books if isinstance(world_books, dict) else {}, f, ensure_ascii=False, indent=2)


def _read_mcp_servers(server) -> list:
    path = os.path.join(server.data_dir, "mcp_servers.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _write_mcp_servers(server, mcp_servers) -> None:
    path = os.path.join(server.data_dir, "mcp_servers.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(mcp_servers if isinstance(mcp_servers, list) else [], f, ensure_ascii=False, indent=2)


def _read_memory_fs(server) -> Dict[str, Any]:
    """读取 data/web/memory_fs.json 全量索引（MemoryFS 单文件 JSON 持久化）。"""
    path = os.path.join(server.data_dir, "memory_fs.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write_memory_fs(server, memory_fs) -> None:
    """把 memory_fs 索引写回 data/web/memory_fs.json，并重置全局单例强制下次重新加载。"""
    path = os.path.join(server.data_dir, "memory_fs.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(memory_fs if isinstance(memory_fs, dict) else {}, f, ensure_ascii=False, indent=2)
    try:
        import nbot.memory.fs as _mfs_mod
        _mfs_mod._memory_fs = None
    except Exception:
        pass


def build_plain_bundle(server) -> Dict[str, Any]:
    ai_models = getattr(server, "ai_models", []) or []
    return {
        "version": BUNDLE_VERSION,
        "type": "nbot_config_bundle",
        "exported_at": datetime.now().isoformat(),
        "source": {
            "hostname": platform.node(),
            "platform": platform.platform(),
        },
        "configs": {
            "settings": deepcopy(getattr(server, "settings", {}) or {}),
            "sessions": deepcopy(getattr(server, "sessions", {}) or {}),
            "ai_config": deepcopy(getattr(server, "ai_config", {}) or {}),
            "ai_models": deepcopy(ai_models),
            "api_keys": _read_api_keys(server),
            "active_model_id": getattr(server, "active_model_id", None),
            "active_models_by_purpose": deepcopy(
                getattr(server, "active_models_by_purpose", {}) or {}
            ),
            "skills": deepcopy(getattr(server, "skills_config", []) or []),
            "tools": deepcopy(getattr(server, "tools_config", []) or []),
            "channels": deepcopy(getattr(server, "channels_config", []) or []),
            "heartbeat": deepcopy(getattr(server, "heartbeat_config", {}) or {}),
            "scheduled_tasks": deepcopy(getattr(server, "scheduled_tasks", []) or []),
            "workflows": deepcopy(getattr(server, "workflows", []) or []),
            "memories": deepcopy(getattr(server, "memories", []) or []),
            "personality": _read_personality(server),
            "custom_personality_presets": deepcopy(
                getattr(server, "custom_personality_presets", []) or []
            ),
            "world_books": _read_world_books(server),
            "mcp_servers": _read_mcp_servers(server),
            "memory_fs": _read_memory_fs(server),
        },
    }


def encrypt_bundle(
    server, password: str, *, plain_bundle: dict[str, Any] | None = None
) -> Dict[str, Any]:
    salt = os.urandom(KDF_SALT_BYTES)
    plaintext = json.dumps(
        build_plain_bundle(server) if plain_bundle is None else plain_bundle,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    token = _fernet_from_password(password, salt).encrypt(plaintext)
    return {
        "version": BUNDLE_VERSION,
        "type": "nbot_config_bundle",
        "encrypted": True,
        "algorithm": "fernet",
        "kdf": "pbkdf2_hmac_sha256",
        "iterations": KDF_ITERATIONS,
        "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        "exported_at": datetime.now().isoformat(),
        "payload": token.decode("ascii"),
    }


def _b64decode_any(value: Any) -> bytes:
    """宽松解码 Base64：兼容 urlsafe / 标准字母表，缺省或带 padding。

    各端实现不完全一致（Android 用 ``Base64.getUrlEncoder()`` 会带 ``=``，
    部分工具会去掉 padding），这里统一补齐后解码，失败返回 ``b""``。
    """
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value).strip()
    elif isinstance(value, (list, tuple)) and all(isinstance(item, int) for item in value):
        # 少数实现把 salt 直接写成整数数组；越界值视为非法，不让 ValueError 逃逸
        try:
            return bytes(value)
        except ValueError:
            return b""
    else:
        data = str(value or "").strip().encode("ascii", "ignore")
    if not data:
        return b""
    data = data.replace(b"\n", b"").replace(b"\r", b"").replace(b" ", b"")
    data += b"=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(data)
    except (binascii.Error, ValueError):
        try:
            return base64.b64decode(data, validate=False)
        except (binascii.Error, ValueError):
            return b""


def _finalize_plain_bundle(plaintext: bytes) -> dict[str, Any]:
    """校验解密后的明文是合法的配置包字典。"""
    try:
        data = json.loads(plaintext.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ConfigTransferError(f"配置包解密成功但内容不是 UTF-8 文本: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigTransferError(f"配置包解密成功但内容不是有效 JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("configs"), dict):
        raise ConfigTransferError("配置包内容无效")
    return data


def decrypt_bundle(bundle: Dict[str, Any], password: str) -> Dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ConfigTransferError("配置包格式无效")
    if bundle.get("type") != "nbot_config_bundle":
        raise ConfigTransferError("不是有效的 NekoBot 配置包")
    if not bundle.get("encrypted"):
        if isinstance(bundle.get("configs"), dict):
            return bundle
        raise ConfigTransferError("配置包未加密且缺少配置内容")

    salt = _b64decode_any(bundle.get("salt", ""))
    if not salt:
        raise ConfigTransferError("配置包缺少有效的 salt（KDF 盐值）")
    payload = str(bundle.get("payload", "")).strip().encode("ascii", "ignore")
    payload += b"=" * (-len(payload) % 4)

    try:
        plaintext = _fernet_from_password(password, salt).decrypt(payload)
    except InvalidToken as exc:
        raise ConfigTransferError("密码错误或配置包已损坏") from exc
    except ConfigTransferError:
        raise
    except Exception as exc:
        raise ConfigTransferError(f"无法解密配置包: {exc}") from exc

    return _finalize_plain_bundle(plaintext)


# ============================================================
# 配置包原始字节加载：按文件头嗅探容器，绝不把二进制当文本读
# （见 issue #16：WebDAV 同步 / 本地导入把二进制密文按 UTF-8 解码导致崩溃）
# ============================================================


def sniff_bundle_format(raw: bytes) -> str:
    """按文件头嗅探配置包容器，返回 ``zip`` / ``gzip`` / ``json`` / ``binary`` / ``empty``。"""
    if not raw:
        return "empty"
    if raw.startswith(_ZIP_MAGICS):
        return "zip"
    if raw.startswith(_GZIP_MAGIC):
        return "gzip"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "json"
    head = raw[3:] if raw.startswith(codecs.BOM_UTF8) else raw
    if head.lstrip(b" \t\r\n")[:1] in (b"{", b"["):
        return "json"
    return "binary"


def _bundle_diagnostic(raw: bytes, fmt: str) -> str:
    preview = " ".join(f"{byte:02x}" for byte in raw[:16]) or "-"
    return f"无法识别的配置包格式（容器={fmt}, 大小={len(raw)} 字节, 头16字节={preview}）"


def _iter_bundle_texts(raw: bytes):
    """依次产出 (编码, 文本)，覆盖 UTF-8(BOM) / UTF-16(BOM) / GB18030。"""
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        encodings = ("utf-16", "gb18030")
    else:
        encodings = _BUNDLE_TEXT_ENCODINGS
    for encoding in encodings:
        try:
            yield encoding, raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError, LookupError):
            continue


def _decrypt_salt_token(
    salt: bytes, candidates: Iterable[bytes], password: str
) -> bytes | None:
    """用给定 salt 依次尝试候选 token，成功返回明文，全部失败返回 None。"""
    fernet = _fernet_from_password(password, salt)
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return fernet.decrypt(candidate)
        except (InvalidToken, binascii.Error, ValueError, TypeError):
            continue
    return None


def _json_value_start(raw: bytes, marker_end: int) -> int | None:
    """从 JSON key 的结尾定位其字符串值的首字节（开引号之后）。"""
    colon = raw.find(b":", marker_end)
    if colon < 0:
        return None
    index = colon + 1
    while index < len(raw) and raw[index] in b" \t\r\n":
        index += 1
    if raw[index : index + 1] != b'"':
        return None
    return index + 1


def _find_value_marker(raw: bytes, marker: bytes, skip_from: int, skip_to: int) -> int:
    """查找 key 标记，跳过落在 [skip_from, skip_to) 区间（如原始 salt 字节）内的伪命中。"""
    search_from = 0
    while True:
        found = raw.find(marker, search_from)
        if found < 0:
            return -1
        if skip_from <= found < skip_to:
            search_from = found + 1
            continue
        return found


def _bundle_from_pseudo_json(raw: bytes, password: str) -> dict[str, Any] | None:
    """解析「salt 以原始字节内嵌在 JSON 字符串里」的配置包。

    部分第三方 / 旧客户端把 16 字节 KDF salt 直接写进 JSON 字符串，salt 值的
    起始引号之后就是随机二进制（issue #16 里从第 11 个字节开始报错即是此形态），
    因此无法按任何编码整体解码，只能在字节层面定位 ``salt`` / ``payload`` 字段。
    两个字段的先后顺序不固定，这里各自独立定位。
    """
    salt_marker = raw.find(b'"salt"')
    if salt_marker < 0:
        return None
    salt_start = _json_value_start(raw, salt_marker + len(b'"salt"'))
    if salt_start is None:
        return None
    salt = raw[salt_start : salt_start + KDF_SALT_BYTES]
    if len(salt) != KDF_SALT_BYTES:
        return None
    salt_end = salt_start + KDF_SALT_BYTES

    payload_marker = _find_value_marker(raw, b'"payload"', salt_start, salt_end)
    if payload_marker < 0:
        return None
    start = _json_value_start(raw, payload_marker + len(b'"payload"'))
    if start is None:
        return None

    candidates: list[bytes] = []
    end = start
    while end < len(raw) and raw[end] in _B64_CHARS:
        end += 1
    plain_base64 = end > start and raw[end : end + 1] == b'"'

    if plain_base64:
        # 常见形态：payload 是 Base64 文本，紧跟结束引号
        candidates.append(raw[start:end])
        candidates.append(_b64decode_any(raw[start:end]))
    else:
        # payload 也可能是原始 token 字节（需要重新编码成 Base64）。
        # 0x22 本身是合法 token 字节，所以「下一个引号」不一定就是结尾；
        # 逐个引号位置尝试，总有一个正好落在真正的结束引号上。
        position = raw.find(b'"', start)
        tries = 0
        while position > start and tries < _PSEUDO_JSON_TOKEN_TRIES:
            encoded = base64.urlsafe_b64encode(raw[start:position])
            tries += 1
            candidates.append(encoded)
            candidates.append(encoded.rstrip(b"="))
            candidates.append(raw[start:position])
            position = raw.find(b'"', position + 1)
        if end > start:
            # 混合内容（Base64 前缀后跟非 Base64 字节）也试一下纯前缀
            candidates.append(raw[start:end])
            candidates.append(_b64decode_any(raw[start:end]))

    plaintext = _decrypt_salt_token(salt, candidates, password)
    if plaintext is None:
        raise ConfigTransferError("密码错误或配置包已损坏")
    return _finalize_plain_bundle(plaintext)


def _looks_like_bundle(bundle: Any) -> bool:
    """粗略判断 JSON 结构是否像配置包（避免把编码错乱的结果当成配置包）。"""
    if not isinstance(bundle, dict):
        return False
    if bundle.get("type") != "nbot_config_bundle":
        return False
    if not bundle.get("encrypted"):
        return isinstance(bundle.get("configs"), dict)
    salt = _b64decode_any(bundle.get("salt", ""))
    payload = str(bundle.get("payload") or "").strip()
    return len(salt) >= 8 and len(payload) >= 32


def _bundle_from_text_json(raw: bytes, password: str) -> dict[str, Any]:
    """按文本 JSON 解析配置包；无法解析时抛 ``_BundleFormatError``。"""
    parse_error: str = ""
    for _encoding, text in _iter_bundle_texts(raw):
        try:
            bundle = json.loads(text)
        except json.JSONDecodeError as exc:
            parse_error = str(exc)
            continue
        if not _looks_like_bundle(bundle):
            # 例如二进制 salt 被强制按 GB18030 解码后的“JSON”，交给字节级解析器
            parse_error = "JSON 结构不像配置包"
            continue
        try:
            return decrypt_bundle(bundle, password)
        except ConfigTransferError as exc:
            # 伪 JSON 的 salt 若恰好是合法文本（如全可打印 ASCII），JSON 层能解析成功，
            # 但用它当 salt 解密必然失败；此处再用字节级 salt 重试一次。
            # 回退也失败时保留原错误（如「配置包内容无效」而不是「密码错误」）。
            try:
                repaired = _bundle_from_pseudo_json(raw, password)
            except ConfigTransferError:
                repaired = None
            if repaired is not None:
                return repaired
            raise exc
    repaired = _bundle_from_pseudo_json(raw, password)
    if repaired is not None:
        return repaired
    raise _BundleFormatError(parse_error or "内容无法按 UTF-8/UTF-16/GB18030 解码")


def _bundle_from_binary(raw: bytes, password: str) -> dict[str, Any]:
    """解析裸二进制布局：前 ``KDF_SALT_BYTES`` 字节为 salt，其后为 Fernet token。"""
    if len(raw) <= KDF_SALT_BYTES + 16:
        raise _BundleFormatError("文件长度不足以构成「salt + token」布局")
    salt = raw[:KDF_SALT_BYTES]
    token = raw[KDF_SALT_BYTES:]
    candidates = [
        token.strip(),
        token,
        # 少数实现直接写入解码后的 Fernet token 字节，需要重新编码成 Base64
        base64.urlsafe_b64encode(token),
        base64.urlsafe_b64encode(token).rstrip(b"="),
        _b64decode_any(token),
    ]
    plaintext = _decrypt_salt_token(salt, candidates, password)
    if plaintext is None:
        raise _BundleFormatError("二进制布局按「salt + Fernet token」解密失败")
    return _finalize_plain_bundle(plaintext)


def _gunzip(raw: bytes) -> bytes:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            data = handle.read(_BUNDLE_MAX_BYTES + 1)
    # 截断的流抛 EOFError，损坏的 deflate/CRC 抛 zlib.error 或 BadGzipFile(OSError)
    except (OSError, EOFError, zlib.error) as exc:
        raise _BundleFormatError(f"GZIP 解压失败: {exc}") from exc
    if len(data) > _BUNDLE_MAX_BYTES:
        raise ConfigTransferError("配置包解压后体积过大（>512MB），已中止")
    return data


def _pick_zip_config_member(names: Iterable[str]) -> str | None:
    """在 ZIP 成员中挑选配置文件：优先 config.nbotcfg，其次任意 .nbotcfg / JSON。"""
    files = [name for name in names if not name.endswith("/")]
    for name in files:
        if os.path.basename(name) == "config.nbotcfg":
            return name
    for name in files:
        if name.lower().endswith(".nbotcfg"):
            return name
    for name in files:
        if os.path.basename(name).lower() in ("config.json", "bundle.json"):
            return name
    return None


def _read_zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        size = archive.getinfo(name).file_size
    except KeyError as exc:
        raise ConfigTransferError(f"ZIP 中缺少成员 {name}") from exc
    if size > _BUNDLE_MAX_BYTES:
        raise ConfigTransferError("配置包解压后体积过大（>512MB），已中止")
    try:
        return archive.read(name)
    except (RuntimeError, zipfile.BadZipFile, zlib.error, OSError) as exc:
        # RuntimeError：ZIP 级加密（与 Fernet 密码无关）；BadZipFile/zlib.error：损坏或 CRC 失败
        raise ConfigTransferError(f"ZIP 成员 {name} 读取失败（可能已加密或损坏）: {exc}") from exc


def _collect_zip_portraits(archive: zipfile.ZipFile, portraits: dict[str, bytes]) -> None:
    """把 ZIP 内的立绘成员收集起来（供嵌套容器的导入路径恢复立绘）。

    ``portraits`` 在所有嵌套层之间共享，因此这里的字节/数量预算也是累计的：
    避免恶意包用多个「单成员合规」的大文件把内存吃满。
    """
    for name in archive.namelist():
        if not name.startswith(_ZIP_PORTRAIT_DIR) or name.endswith("/"):
            continue
        if name in portraits:
            continue
        if len(portraits) >= _PORTRAITS_MAX_FILES:
            _log.warning("[ConfigTransfer] 立绘成员数超过 %d，跳过其余成员", _PORTRAITS_MAX_FILES)
            return
        try:
            size = archive.getinfo(name).file_size
        except KeyError:
            continue
        used = sum(len(item) for item in portraits.values())
        if size > _BUNDLE_MAX_BYTES or used + size > _PORTRAITS_MAX_BYTES:
            _log.warning(
                "[ConfigTransfer] 跳过超预算立绘成员 %s（%d bytes，已用 %d/%d）",
                name,
                size,
                used,
                _PORTRAITS_MAX_BYTES,
            )
            continue
        try:
            data = archive.read(name)
        except (RuntimeError, zipfile.BadZipFile, zlib.error, OSError) as exc:
            _log.warning("[ConfigTransfer] 立绘成员 %s 读取失败: %s", name, exc)
            continue
        portraits[name] = data


def _binary_fallback(raw: bytes, password: str) -> dict[str, Any] | None:
    """容器魔数命中但解包失败时的回退：salt 恰好以魔数开头的情况。"""
    try:
        return _bundle_from_binary(raw, password)
    except _BundleFormatError:
        return None


def load_bundle_bytes(
    raw: bytes,
    password: str,
    *,
    _depth: int = 0,
    _portraits: dict[str, bytes] | None = None,
) -> dict[str, Any]:
    """把任意来源的 ``.nbotcfg`` 原始字节解析成已解密的配置字典。

    全程使用二进制读取，按文件头嗅探容器后再解码，绝不把二进制密文当文本读：

    1. ``PK\\x03\\x04`` → ZIP，取出 ``config.nbotcfg`` 成员后递归；
    2. ``\\x1f\\x8b`` → GZIP，解压后递归；
    3. 文本 JSON（UTF-8/BOM/UTF-16/GB18030）→ ``decrypt_bundle``；
    4. 回退：salt 以原始字节内嵌的伪 JSON，或裸二进制「16 字节 salt + token」。

    传入 ``_portraits`` 时，途经的 ZIP 容器会把 ``portraits/`` 成员收集进去，
    便于调用方恢复立绘（见 ``load_bundle_bytes_with_portraits``）。

    解密成功但内容不合法、密码错误、体积超限等一律抛 ``ConfigTransferError``。
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise ConfigTransferError("配置包内容无效")
    raw = bytes(raw)
    if not raw:
        raise ConfigTransferError("配置包内容为空")

    fmt = sniff_bundle_format(raw)
    if fmt == "gzip":
        if _depth >= _BUNDLE_MAX_DEPTH:
            raise ConfigTransferError("配置包嵌套层数过多，已中止")
        try:
            inner = _gunzip(raw)
        except _BundleFormatError as exc:
            # salt 前两字节恰好是 \x1f\x8b 时会被误判成 GZIP，回退按二进制布局解析
            fallback = _binary_fallback(raw, password)
            if fallback is not None:
                return fallback
            raise ConfigTransferError(
                f"配置包 GZIP 解压失败（文件可能被截断或损坏）: {exc}"
            ) from exc
        return load_bundle_bytes(inner, password, _depth=_depth + 1, _portraits=_portraits)

    if fmt == "zip":
        if _depth >= _BUNDLE_MAX_DEPTH:
            raise ConfigTransferError("配置包嵌套层数过多，已中止")
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                member = _pick_zip_config_member(archive.namelist())
                if not member:
                    raise ConfigTransferError("ZIP 中未找到配置文件 (config.nbotcfg)")
                inner = _read_zip_member(archive, member)
                if _portraits is not None:
                    _collect_zip_portraits(archive, _portraits)
        except zipfile.BadZipFile as exc:
            # salt 恰好以 PK\x03\x04 开头时同理回退
            fallback = _binary_fallback(raw, password)
            if fallback is not None:
                return fallback
            raise ConfigTransferError(f"ZIP 文件损坏: {exc}") from exc
        return load_bundle_bytes(inner, password, _depth=_depth + 1, _portraits=_portraits)

    failures: list[str] = []
    for parser in (_bundle_from_text_json, _bundle_from_binary):
        try:
            return parser(raw, password)
        except _BundleFormatError as exc:
            failures.append(str(exc))
    message = _bundle_diagnostic(raw, fmt)
    detail = "；".join(item for item in failures if item)
    if detail:
        message = f"{message}：{detail}"
    message = f"{message}；若这确实是加密配置包，请确认加密密码是否正确"
    raise ConfigTransferError(message)


def load_bundle_bytes_with_portraits(
    raw: bytes, password: str
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """解析配置包字节，并返回其中 ZIP 容器携带的立绘文件。

    GZIP 包裹的 ZIP、ZIP 套 ZIP 等嵌套形态同样会收集立绘，避免
    「配置导入成功但立绘引用指向不存在的文件」。
    """
    portraits: dict[str, bytes] = {}
    bundle = load_bundle_bytes(raw, password, _portraits=portraits)
    return bundle, portraits


def _save_imported_configs(server, touched: Iterable[str]) -> None:
    for data_type in touched:
        if data_type in SAVE_TYPES:
            server._save_data(data_type)


def apply_bundle(server, bundle: Dict[str, Any], *, overwrite: bool = True) -> Dict[str, Any]:
    configs = bundle.get("configs") or {}
    if not isinstance(configs, dict):
        raise ConfigTransferError("配置包缺少 configs")

    imported = []
    skipped = []
    touched = set()

    def assign(config_key: str, attr: str, default: Any, save_type: str = None):
        if config_key not in configs:
            skipped.append(config_key)
            return
        value = configs.get(config_key)
        if value is None:
            value = default
        if not overwrite:
            current = getattr(server, attr, None)
            if current:
                skipped.append(config_key)
                return
        clean_value = _clean_json(value)
        if attr == "sessions" and isinstance(getattr(server, "sessions", None), dict):
            server.sessions.clear()
            server.sessions.update(clean_value if isinstance(clean_value, dict) else {})
            if getattr(server, "PROGRESS_CARD_AVAILABLE", False) and getattr(
                server, "progress_card_manager", None
            ):
                server.progress_card_manager.set_sessions(server.sessions)
            if getattr(server, "TODO_CARD_AVAILABLE", False) and getattr(
                server, "todo_card_manager", None
            ):
                server.todo_card_manager.set_sessions(server.sessions)
        else:
            setattr(server, attr, clean_value)
        imported.append(config_key)
        if save_type:
            touched.add(save_type)

    assign("settings", "settings", {}, "settings")
    assign("sessions", "sessions", {}, "sessions")
    assign("ai_config", "ai_config", {}, "ai_config")
    assign("ai_models", "ai_models", [], "ai_models")
    assign("active_model_id", "active_model_id", None, "ai_models")
    assign("active_models_by_purpose", "active_models_by_purpose", {}, "ai_models")
    assign("skills", "skills_config", [], "skills")
    assign("tools", "tools_config", [], "tools")
    assign("channels", "channels_config", [], "channels")
    assign("heartbeat", "heartbeat_config", {}, "heartbeat")
    assign("scheduled_tasks", "scheduled_tasks", [], "scheduled_tasks")
    assign("workflows", "workflows", [], "workflows")
    assign("memories", "memories", [], "memories")
    assign(
        "custom_personality_presets",
        "custom_personality_presets",
        [],
        "custom_personality_presets",
    )

    if "personality" in configs:
        personality = configs.get("personality") if isinstance(configs.get("personality"), dict) else {}
        if overwrite or not getattr(server, "personality", None):
            _write_personality(server, _clean_json(personality))
            imported.append("personality")
        else:
            skipped.append("personality")

    if "api_keys" in configs:
        api_keys = configs.get("api_keys")
        if overwrite or not _read_api_keys(server):
            _write_api_keys(server, _clean_json(api_keys if isinstance(api_keys, list) else []))
            imported.append("api_keys")
        else:
            skipped.append("api_keys")

    if "world_books" in configs:
        world_books = configs.get("world_books")
        if overwrite or not _read_world_books(server):
            _write_world_books(server, _clean_json(world_books if isinstance(world_books, dict) else {}))
            imported.append("world_books")
        else:
            skipped.append("world_books")

    if "mcp_servers" in configs:
        mcp_servers = configs.get("mcp_servers")
        if overwrite or not _read_mcp_servers(server):
            _write_mcp_servers(server, _clean_json(mcp_servers if isinstance(mcp_servers, list) else []))
            imported.append("mcp_servers")
        else:
            skipped.append("mcp_servers")

    if "memory_fs" in configs:
        memory_fs = configs.get("memory_fs")
        if overwrite or not _read_memory_fs(server):
            _write_memory_fs(server, _clean_json(memory_fs if isinstance(memory_fs, dict) else {}))
            imported.append("memory_fs")
        else:
            skipped.append("memory_fs")

    _save_imported_configs(server, touched)

    active_chat_model = getattr(server, "active_model_id", None)
    if active_chat_model:
        try:
            server._apply_ai_model(active_chat_model, purpose="chat")
        except Exception:
            pass
    try:
        from nbot.services.ai import refresh_runtime_ai_config

        refresh_runtime_ai_config()
    except Exception:
        pass

    return {
        "imported": imported,
        "skipped": skipped,
        "exported_at": bundle.get("exported_at"),
        "source": bundle.get("source") or {},
    }


# ==================== ZIP 导出/导入（含立绘）====================

# 立绘本地存储目录前缀
_PORTRAIT_URL_PREFIX = "/static/uploads/portraits/"
# 立绘文件在ZIP中的存放目录
_ZIP_PORTRAIT_DIR = "portraits/"


def _collect_portrait_paths(server, plain_bundle: Dict[str, Any]) -> Dict[str, str]:
    """从配置包中扫描所有立绘URL，返回 {zip内路径: 本地文件绝对路径}

    扫描范围：
    - personality.portrait（主角色立绘）
    - sessions[*].sender_portrait（各会话立绘）
    - custom_personality_presets 中的 portrait 字段
    """
    portraits = {}
    configs = plain_bundle.get("configs", {})
    static_root = os.path.join(server.base_dir, "nbot", "web", "static")

    def add_portrait(url: str) -> None:
        if not url or not isinstance(url, str):
            return
        if not url.startswith(_PORTRAIT_URL_PREFIX):
            return
        filename = os.path.basename(url)
        relative_path = url[len("/static/"):]
        local_path = os.path.join(static_root, relative_path)
        if not os.path.isfile(local_path):
            return
        zip_path = f"{_ZIP_PORTRAIT_DIR}{filename}"
        if zip_path not in portraits:
            portraits[zip_path] = local_path

    # 1. 主角色 personality 立绘
    personality = configs.get("personality")
    if isinstance(personality, dict):
        add_portrait(personality.get("portrait"))

    # 2. 各会话的 sender_portrait
    sessions = configs.get("sessions")
    if isinstance(sessions, dict):
        for sess in sessions.values():
            if isinstance(sess, dict):
                add_portrait(sess.get("sender_portrait"))

    # 3. 自定义角色预设中的 portrait
    presets = configs.get("custom_personality_presets")
    if isinstance(presets, list):
        for preset in presets:
            if isinstance(preset, dict):
                add_portrait(preset.get("portrait"))

    _log.info(f"[ConfigTransfer] 导出ZIP，收集到 {len(portraits)} 张立绘")
    return portraits


def build_zip_bundle(server, password: str) -> io.BytesIO:
    """构建包含配置+立绘的 ZIP 文件（内存中），返回 BytesIO 对象"""
    encrypted = encrypt_bundle(server, password)
    portraits = _collect_portrait_paths(server, build_plain_bundle(server))

    memory_file = io.BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        # 写入加密配置 JSON
        config_json = json.dumps(encrypted, ensure_ascii=False, indent=2)
        zf.writestr("config.nbotcfg", config_json)

        # 写入立绘清单（记录哪些配置字段对应哪个立绘文件）
        manifest = {"version": 2, "type": "nbot_config_zip", "portraits": list(portraits.keys())}
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False))

        # 写入立绘图片文件
        for zip_path, local_path in portraits.items():
            zf.write(local_path, zip_path)

    memory_file.seek(0)
    return memory_file


def extract_zip_bundle(zip_bytes: bytes, password: str) -> Tuple[Dict[str, Any], Dict[str, bytes]]:
    """从 ZIP 中解析出配置和立绘文件

    Returns:
        (解密后的配置字典, {zip内路径: 文件二进制数据})
    """
    return load_bundle_bytes_with_portraits(zip_bytes, password)


def restore_portraits_from_zip(server, portraits: Dict[str, bytes], bundle: Dict[str, Any]) -> int:
    """将 ZIP 中的立绘文件恢复到本地，并更新配置中的 URL 引用

    Args:
        server: 服务端实例
        portraits: {zip路径: 文件二进制数据}
        bundle: 已解密的配置字典（会被原地修改以更新portrait URL）

    Returns:
        成功恢复的立绘数量
    """
    static_root = os.path.join(server.base_dir, "nbot", "web", "static")
    upload_dir = os.path.join(static_root, "uploads", "portraits")
    os.makedirs(upload_dir, exist_ok=True)

    # 建立 zip路径 → 新URL 的映射
    url_map = {}  # 旧文件名 → 新URL
    restored_count = 0

    for zip_path, data in portraits.items():
        old_filename = os.path.basename(zip_path)
        ext = os.path.splitext(old_filename)[1] or ".png"
        new_filename = f"portrait_{os.urandom(8).hex()}{ext}"
        dest_path = os.path.join(upload_dir, new_filename)

        try:
            with open(dest_path, 'wb') as f:
                f.write(data)
            new_url = f"{_PORTRAIT_URL_PREFIX}{new_filename}"
            url_map[old_filename] = new_url
            restored_count += 1
        except Exception:
            continue

    # 更新配置中的 portrait URL
    configs = bundle.get("configs", {})
    if not url_map or not isinstance(configs, dict):
        return restored_count

    def update_portrait_url(obj: Any) -> None:
        """递归更新对象中 portrait / sender_portrait 的值"""
        if isinstance(obj, dict):
            for key in ("portrait", "sender_portrait"):
                old_val = obj.get(key)
                if isinstance(old_val, str) and old_val:
                    old_basename = os.path.basename(old_val)
                    if old_basename in url_map:
                        obj[key] = url_map[old_basename]
            # 递归处理嵌套结构
            for v in obj.values():
                update_portrait_url(v)
        elif isinstance(obj, list):
            for item in obj:
                update_portrait_url(item)

    update_portrait_url(configs)
    return restored_count
