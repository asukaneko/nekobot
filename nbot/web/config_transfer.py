import base64
import io
import json
import logging
import os
import platform
import re
import zipfile
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Iterable, List, Tuple

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


class ConfigTransferError(ValueError):
    pass


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


def decrypt_bundle(bundle: Dict[str, Any], password: str) -> Dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ConfigTransferError("配置包格式无效")
    if bundle.get("type") != "nbot_config_bundle":
        raise ConfigTransferError("不是有效的 NekoBot 配置包")
    if not bundle.get("encrypted"):
        if isinstance(bundle.get("configs"), dict):
            return bundle
        raise ConfigTransferError("配置包未加密且缺少配置内容")

    try:
        salt = base64.urlsafe_b64decode(str(bundle.get("salt", "")).encode("ascii"))
        payload = str(bundle.get("payload", "")).encode("ascii")
        plaintext = _fernet_from_password(password, salt).decrypt(payload)
        data = json.loads(plaintext.decode("utf-8"))
    except InvalidToken as exc:
        raise ConfigTransferError("密码错误或配置包已损坏") from exc
    except Exception as exc:
        raise ConfigTransferError(f"无法解密配置包: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("configs"), dict):
        raise ConfigTransferError("配置包内容无效")
    return data


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
    with zipfile.ZipFile(io.BytesIO(zip_bytes), 'r') as zf:
        namelist = zf.namelist()

        # 查找配置文件
        config_filename = None
        for name in namelist:
            if name == "config.nbotcfg":
                config_filename = name
                break
            # 兼容旧版 .nbotcfg 直接打ZIP的情况
            if name.endswith(".nbotcfg"):
                config_filename = name
                break

        if not config_filename:
            raise ConfigTransferError("ZIP 中未找到配置文件 (config.nbotcfg)")

        # 解析并解密配置
        raw_config = json.loads(zf.read(config_filename).decode("utf-8"))
        bundle = decrypt_bundle(raw_config, password)

        # 收集所有立绘文件
        portraits = {}
        for name in namelist:
            if name.startswith(_ZIP_PORTRAIT_DIR):
                portraits[name] = zf.read(name)

        return bundle, portraits


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
