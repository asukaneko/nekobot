"""酒馆（SillyTavern）角色卡互转

支持 ``chara_card_v2`` / ``chara_card_v3`` JSON 与 PNG tEXt 内嵌卡片的导入，
以及把本项目角色卡导出为酒馆兼容格式。

PNG tEXt 关键词约定（酒馆生态通用）：

- ``chara``：base64(JSON)，v2 卡片，生态广泛支持
- ``ccv3``：base64(JSON)，v3 卡片，优先读取

字段映射（酒馆 → 本项目）：

===================  ==========================  ===============================
酒馆字段              本项目字段                    说明
===================  ==========================  ===============================
name                 name
description          description
personality          personality
scenario             scenario
first_mes            firstMessage
mes_example          exampleDialogues
system_prompt        systemPrompt
alternate_greetings  alternateGreetings           多条开场白
character_book       characterBook               内嵌世界书（转为世界书条目）
tags                 tags
creator_notes        tavern.creatorNotes         仅保留在元数据
post_history_instructions  tavern.postHistoryInstructions
===================  ==========================  ===============================

酒馆没有 ``basicInfo`` / ``rules`` / ``responseFormat`` / ``state`` 概念，
导出时统一放在 ``data.extensions.nekobot`` 下，保证往返不丢字段。
"""

import base64
import binascii
import json
import re
import struct
import zlib
from typing import Any, Dict, Iterable, List, Optional, Tuple

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

SPEC_V2 = "chara_card_v2"
SPEC_V3 = "chara_card_v3"

PNG_TEXT_KEY_ORDER = ("ccv3", "chara")

DEFAULT_STATE = {
    "affection": 50,
    "trust": 50,
    "familiarity": 30,
    "dependency": 30,
    "security": 50,
    "jealousy": 0,
    "mood": "开心",
}

# 酒馆 position → 本项目 WorldBookEntry.position
_TAVERN_POSITION_MAP = {
    "before_char": "before_char",
    "after_char": "after_char",
    "before_an": "before_an",
    "after_an": "after_an",
    "at_depth": "at_depth",
    "0": "before_char",
    "1": "after_char",
    "2": "before_an",
    "3": "after_an",
    "4": "at_depth",
}

_POSITION_TO_TAVERN = {
    "before_char": "before_char",
    "after_char": "after_char",
    "before_an": "after_char",
    "after_an": "after_char",
    "at_depth": "at_depth",
    "": "before_char",
}


class TavernCardError(ValueError):
    """角色卡解析失败"""


# --------------------------------------------------------------------------- #
# PNG tEXt 处理
# --------------------------------------------------------------------------- #


def _iter_png_chunks(data: bytes) -> Iterable[Tuple[bytes, bytes]]:
    if not data.startswith(PNG_SIGNATURE):
        return
    pos = len(PNG_SIGNATURE)
    total = len(data)
    while pos + 8 <= total:
        length = int.from_bytes(data[pos:pos + 4], "big")
        chunk_type = data[pos + 4:pos + 8]
        payload_start = pos + 8
        payload_end = payload_start + length
        if payload_end + 4 > total:
            return
        yield chunk_type, data[payload_start:payload_end]
        pos = payload_end + 4


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)


def read_png_text_chunks(data: bytes) -> Dict[str, str]:
    """读取 PNG 的 tEXt / zTXt / iTXt 文本块，返回 {keyword: text}。"""
    texts: Dict[str, str] = {}
    for chunk_type, payload in _iter_png_chunks(data):
        try:
            if chunk_type == b"tEXt":
                if b"\x00" not in payload:
                    continue
                key, value = payload.split(b"\x00", 1)
                texts[key.decode("latin-1", "replace")] = value.decode("latin-1", "replace")
            elif chunk_type == b"zTXt":
                key, rest = payload.split(b"\x00", 1)
                if not rest:
                    continue
                texts[key.decode("latin-1", "replace")] = zlib.decompress(rest[1:]).decode(
                    "latin-1", "replace"
                )
            elif chunk_type == b"iTXt":
                if b"\x00" not in payload:
                    continue
                key_bytes, rest = payload.split(b"\x00", 1)
                # rest = compression_flag(1) compression_method(1) lang\0 translated\0 text
                if len(rest) < 2:
                    continue
                compressed = rest[0] == 1
                rest = rest[2:]
                parts = rest.split(b"\x00", 2)
                if len(parts) != 3:
                    continue
                text_bytes = parts[2]
                if compressed:
                    text_bytes = zlib.decompress(text_bytes)
                texts[key_bytes.decode("latin-1", "replace")] = text_bytes.decode(
                    "utf-8", "replace"
                )
        except (ValueError, zlib.error):
            continue
    return texts


def is_png(data: bytes) -> bool:
    return bool(data) and data.startswith(PNG_SIGNATURE)


def insert_png_text_chunks(data: bytes, payloads: Dict[str, str]) -> bytes:
    """把文本块写入 PNG（同关键词的旧块会被替换），返回新的 PNG 字节。"""
    if not is_png(data):
        raise TavernCardError("不是有效的 PNG 文件")

    chunks = list(_iter_png_chunks(data))
    if not chunks or chunks[0][0] != b"IHDR":
        raise TavernCardError("PNG 缺少 IHDR 块，文件可能已损坏")

    out: List[bytes] = [PNG_SIGNATURE]
    inserted = False
    for chunk_type, payload in chunks:
        if chunk_type in (b"tEXt", b"zTXt", b"iTXt") and b"\x00" in payload:
            key = payload.split(b"\x00", 1)[0].decode("latin-1", "replace")
            if key in payloads:
                continue
        out.append(_png_chunk(chunk_type, payload))
        if chunk_type == b"IHDR" and not inserted:
            for key, value in payloads.items():
                if not value:
                    continue
                out.append(_png_text_chunk(key, value))
            inserted = True

    if not inserted:
        raise TavernCardError("PNG 缺少 IHDR 块，文件可能已损坏")
    return b"".join(out)


def _png_text_chunk(key: str, value: str) -> bytes:
    """构造文本块：ASCII 内容用 tEXt，含非 ASCII 时用 iTXt（UTF-8）。"""
    key_bytes = key.encode("latin-1", "replace")
    try:
        value_bytes = value.encode("latin-1")
    except UnicodeEncodeError:
        body = key_bytes + b"\x00" + b"\x00" + b"\x00" + b"\x00" + b"\x00" + value.encode("utf-8")
        return _png_chunk(b"iTXt", body)
    return _png_chunk(b"tEXt", key_bytes + b"\x00" + value_bytes)


def build_placeholder_png(width: int = 400, height: int = 600, rgb=(28, 26, 38)) -> bytes:
    """生成纯色占位 PNG，用于没有立绘时导出卡片。"""
    width = max(1, int(width))
    height = max(1, int(height))
    row = b"\x00" + bytes(rgb) * width
    raw = row * height
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def _decode_payload_text(text: str) -> Optional[Dict[str, Any]]:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("data:"):
        # data:image/png;base64,xxx
        text = text.split(",", 1)[-1]
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    compact = "".join(text.split())
    padding = (-len(compact)) % 4
    try:
        raw = base64.b64decode(compact + "=" * padding, validate=False)
    except (binascii.Error, ValueError):
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            data = json.loads(raw.decode(encoding))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def extract_card_payload(png_data: bytes) -> Optional[Dict[str, Any]]:
    """从 PNG 的 tEXt 块中取出角色卡 JSON（优先 ccv3，其次 chara）。"""
    texts = read_png_text_chunks(png_data)
    for key in PNG_TEXT_KEY_ORDER:
        payload = _decode_payload_text(texts.get(key, ""))
        if payload:
            return payload
    return None


def detect_card_format(raw: Dict[str, Any]) -> str:
    """判断卡片来源格式：tavern_v3 / tavern_v2 / tavern_v1 / native / unknown。"""
    if not isinstance(raw, dict):
        return "unknown"
    spec = str(raw.get("spec") or "").strip().lower()
    if spec == SPEC_V3:
        return "tavern_v3"
    if spec == SPEC_V2:
        return "tavern_v2"

    data = raw.get("data") if isinstance(raw.get("data"), dict) else None
    if data is not None and ("first_mes" in data or "mes_example" in data):
        return "tavern_v2"
    if "first_mes" in raw or "mes_example" in raw:
        return "tavern_v1"
    if any(key in raw for key in ("firstMessage", "basicInfo", "exampleDialogues", "systemPrompt")):
        return "native"
    if "name" in raw or "description" in raw:
        return "native"
    return "unknown"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(_as_text(item) for item in value if item is not None)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return default


def _normalize_position(value: Any) -> str:
    if isinstance(value, (int, float)):
        value = str(int(value))
    key = str(value or "").strip().lower()
    return _TAVERN_POSITION_MAP.get(key, "")


def character_book_entry_to_world_book_entry(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把酒馆 character_book 条目转为本项目 WorldBookEntry 字段字典。"""
    if not isinstance(raw, dict):
        raw = {}

    extensions = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
    position = _normalize_position(extensions.get("position", raw.get("position", "")))
    depth = _as_int(extensions.get("depth", raw.get("depth", 4)), 4)

    primary_keys = [str(k) for k in _as_list(raw.get("keys") or raw.get("key")) if str(k).strip()]
    secondary_keys = [
        str(k) for k in _as_list(raw.get("secondary_keys") or raw.get("secondaryKeys"))
        if str(k).strip()
    ]
    match_mode = "all" if (_as_bool(raw.get("selective")) and secondary_keys) else "any"

    name = _as_text(raw.get("name") or raw.get("comment") or "").strip()
    probability = _as_int(extensions.get("probability", raw.get("probability", 100)), 100)
    use_regex = _as_bool(extensions.get("use_regex", raw.get("use_regex")))
    match_whole_words = _as_bool(
        extensions.get("match_whole_words", raw.get("match_whole_words"))
    )
    return {
        "name": name,
        "keywords": primary_keys + secondary_keys,
        "content": _as_text(raw.get("content")),
        "enabled": _as_bool(raw.get("enabled", True), True) and not _as_bool(raw.get("disable")),
        "priority": _as_int(raw.get("insertion_order"), 0),
        "case_sensitive": _as_bool(raw.get("case_sensitive")),
        "match_mode": match_mode,
        "probability": max(0, min(100, probability)),
        "use_regex": use_regex,
        "match_whole_words": match_whole_words,
        "trigger_sources": ["user"],
        "always_on": _as_bool(raw.get("constant")),
        "state_triggers": {},
        "cooldown_turns": 0,
        "max_injections_per_session": 0,
        "tags": [],
        "entry_type": "lore",
        "weight": 0,
        "position": position,
        "depth": max(0, depth) if position in {"before_an", "after_an", "at_depth"} else 4,
    }


def normalize_character_book(raw: Any) -> Optional[Dict[str, Any]]:
    """归一化酒馆内嵌世界书，返回 {name, description, entries, ...}。"""
    if not isinstance(raw, dict):
        return None

    raw_entries = raw.get("entries")
    if isinstance(raw_entries, dict):
        raw_entries = list(raw_entries.values())
    if not isinstance(raw_entries, list):
        raw_entries = []

    entries = [
        character_book_entry_to_world_book_entry(item)
        for item in raw_entries
        if isinstance(item, dict)
    ]
    entries = [entry for entry in entries if entry["content"].strip()]
    if not entries and not raw.get("name"):
        return None

    extensions = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
    return {
        "name": _as_text(raw.get("name") or "").strip(),
        "description": _as_text(raw.get("description")),
        "scan_depth": _as_int(raw.get("scan_depth"), 0),
        "token_budget": _as_int(raw.get("token_budget"), 0),
        "recursive_scanning": _as_bool(raw.get("recursive_scanning")),
        "extensions": extensions,
        "entries": entries,
    }


def _collect_book_warnings(book: Optional[Dict[str, Any]]) -> List[str]:
    warnings: List[str] = []
    if not book:
        return warnings
    for entry in book.get("entries", []):
        label = entry.get("name") or "未命名"
        if not entry.get("keywords") and not entry.get("always_on"):
            warnings.append(f"条目「{label}」没有关键词，也不会常驻，导入后不会自动触发")
        probability = entry.get("probability", 100)
        if isinstance(probability, int) and probability < 100:
            warnings.append(
                f"条目「{label}」设置了 {probability}% 触发概率，部分轮次不会注入"
            )
        if entry.get("use_regex"):
            if entry.get("match_whole_words"):
                warnings.append(f"条目「{label}」同时启用正则与整词匹配，整词设置将被忽略")
            for pattern in entry.get("keywords") or []:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    warnings.append(f"条目「{label}」的正则关键词「{pattern}」无法编译：{exc}")
    return warnings


def normalize_tavern_card(raw: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """把任意来源（原生 / 酒馆 v1-v3）的角色卡归一化为本项目字段。

    Returns:
        (native_card, meta): native_card 可直接用于前端编辑与保存；
        meta 包含来源格式、酒馆元数据与告警。
    """
    if not isinstance(raw, dict):
        raise TavernCardError("角色卡内容不是 JSON 对象")

    card_format = detect_card_format(raw)
    if card_format in {"tavern_v2", "tavern_v3"}:
        data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    else:
        data = raw

    warnings: List[str] = []
    tavern_meta: Dict[str, Any] = {}
    if card_format.startswith("tavern_"):
        tavern_meta = {
            "spec": _as_text(raw.get("spec")),
            "specVersion": _as_text(raw.get("spec_version") or raw.get("specVersion")),
            "creatorNotes": _as_text(data.get("creator_notes")),
            "creator": _as_text(data.get("creator")),
            "characterVersion": _as_text(data.get("character_version")),
            "postHistoryInstructions": _as_text(data.get("post_history_instructions")),
            "nickname": _as_text(data.get("nickname")),
            "groupOnlyGreetings": [
                _as_text(item) for item in _as_list(data.get("group_only_greetings"))
                if _as_text(item).strip()
            ],
            "sourceUrl": _as_text(data.get("source_url") or raw.get("source_url")),
            "extensions": data.get("extensions") if isinstance(data.get("extensions"), dict) else {},
        }

    nekobot_ext = {}
    if isinstance(tavern_meta.get("extensions"), dict):
        nekobot_ext = tavern_meta["extensions"].get("nekobot") or {}

    alternate_greetings = [
        _as_text(item).strip()
        for item in _as_list(data.get("alternate_greetings") or data.get("alternateGreetings"))
        if _as_text(item).strip()
    ]

    character_book = normalize_character_book(data.get("character_book"))
    warnings.extend(_collect_book_warnings(character_book))

    rules = _as_list(data.get("rules") or nekobot_ext.get("rules"))
    rules = [_as_text(rule) for rule in rules if _as_text(rule).strip()]

    state = data.get("state") if isinstance(data.get("state"), dict) else None
    if not state and isinstance(nekobot_ext.get("state"), dict):
        state = nekobot_ext["state"]

    first_message = _as_text(data.get("first_mes") or data.get("firstMessage"))
    example_dialogues = _as_text(data.get("mes_example") or data.get("exampleDialogues"))

    native: Dict[str, Any] = {
        "name": _as_text(data.get("name")).strip(),
        "description": _as_text(data.get("description")),
        "personality": _as_text(data.get("personality")),
        "scenario": _as_text(data.get("scenario")),
        "firstMessage": first_message,
        "exampleDialogues": example_dialogues,
        "basicInfo": _as_text(data.get("basicInfo") or nekobot_ext.get("basicInfo")),
        "responseFormat": _as_text(data.get("responseFormat") or nekobot_ext.get("responseFormat")),
        "rules": rules,
        "tags": [_as_text(tag) for tag in _as_list(data.get("tags")) if _as_text(tag).strip()],
        "systemPrompt": _as_text(data.get("system_prompt") or data.get("systemPrompt")),
        "alternateGreetings": alternate_greetings,
        "characterBook": character_book,
        "tavern": tavern_meta,
        "avatar": "",
        "portrait": "",
        "state": dict(state) if state else dict(DEFAULT_STATE),
    }

    if not native["name"]:
        raise TavernCardError("角色卡缺少 name 字段")
    if not native["firstMessage"] and alternate_greetings:
        warnings.append("角色卡没有主开场白，已从备用开场白中取第一条作为开场白")
        native["firstMessage"] = alternate_greetings[0]
    if not any(
        [native["description"], native["personality"], native["scenario"], native["basicInfo"]]
    ):
        warnings.append("角色卡缺少设定文本（description/personality/scenario 均为空）")

    meta = {
        "format": card_format,
        "tavern": tavern_meta,
        "warnings": warnings,
        "alternate_greetings": alternate_greetings,
        "character_book": character_book,
    }
    return native, meta


def parse_card_bytes(filename: str, blob: bytes) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """解析上传的角色卡文件（.png / .json），返回 (native_card, meta)。"""
    if not blob:
        raise TavernCardError("文件内容为空")

    lower = (filename or "").lower()
    if is_png(blob) or lower.endswith(".png"):
        payload = extract_card_payload(blob)
        if not payload:
            raise TavernCardError("PNG 中未找到角色卡数据（需包含 chara 或 ccv3 文本块）")
        native, meta = normalize_tavern_card(payload)
        meta["format"] = "png_" + str(meta.get("format") or "tavern")
        return native, meta

    if lower.endswith(".json") or blob.lstrip()[:1] in (b"{", b"\xef"):
        try:
            raw = json.loads(blob.decode("utf-8-sig"))
        except UnicodeDecodeError as exc:
            raise TavernCardError("文件编码不支持，请使用 UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise TavernCardError("文件不是有效的 JSON") from exc
        return normalize_tavern_card(raw)

    raise TavernCardError("不支持的文件格式，请上传 .png 或 .json 角色卡")


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #


def world_book_entry_to_tavern_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把本项目世界书条目转成酒馆 character_book 条目。"""
    position = str(entry.get("position") or "")
    depth = _as_int(entry.get("depth"), 4)
    keywords = [str(item) for item in _as_list(entry.get("keywords")) if str(item).strip()]
    match_mode = str(entry.get("match_mode") or "any")
    probability = max(0, min(100, _as_int(entry.get("probability"), 100)))
    use_regex = bool(entry.get("use_regex", False))
    match_whole_words = bool(entry.get("match_whole_words", False))

    return {
        "keys": keywords,
        "secondary_keys": [],
        "content": _as_text(entry.get("content")),
        "enabled": bool(entry.get("enabled", True)),
        "insertion_order": _as_int(entry.get("priority"), 0),
        "case_sensitive": bool(entry.get("case_sensitive")),
        "name": _as_text(entry.get("name")),
        "comment": _as_text(entry.get("name")),
        "selective": match_mode == "all",
        "constant": bool(entry.get("always_on")),
        "use_regex": use_regex,
        "match_whole_words": match_whole_words,
        "position": _POSITION_TO_TAVERN.get(position, "before_char"),
        "extensions": {
            "position": _POSITION_TO_TAVERN.get(position, "before_char"),
            "depth": depth,
            "probability": probability,
            "use_regex": use_regex,
            "match_whole_words": match_whole_words,
            "exclude_recursion": False,
            "nekobot_position": position,
            "nekobot_match_mode": match_mode,
        },
    }


def to_tavern_character_book(book: Dict[str, Any]) -> Dict[str, Any]:
    entries = [_as_dict_entry(item) for item in _as_list(book.get("entries"))]
    return {
        "name": _as_text(book.get("name")),
        "description": _as_text(book.get("description")),
        "scan_depth": _as_int(book.get("scan_depth"), 0),
        "token_budget": _as_int(book.get("token_budget"), 0),
        "recursive_scanning": bool(book.get("recursive_scanning")),
        "extensions": book.get("extensions") if isinstance(book.get("extensions"), dict) else {},
        "entries": [world_book_entry_to_tavern_entry(entry) for entry in entries],
    }


def _as_dict_entry(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    if hasattr(item, "to_dict"):
        try:
            data = item.to_dict()
            if isinstance(data, dict):
                return data
        except Exception:  # pragma: no cover - defensive
            return {}
    return {}


def to_tavern_card(
    native: Dict[str, Any],
    version: str = "v3",
    character_book: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """把本项目角色卡导出为酒馆 chara_card_v2 / v3 结构。

    Args:
        native: 本项目角色卡字典（firstMessage/basicInfo/... 风格）
        version: "v2" 或 "v3"
        character_book: 内嵌世界书（未传时读 native["characterBook"]）
    """
    if not isinstance(native, dict):
        raise TavernCardError("角色卡数据不是对象")

    name = _as_text(native.get("name")).strip()
    if not name:
        raise TavernCardError("角色卡缺少名称")

    tavern_meta = native.get("tavern") if isinstance(native.get("tavern"), dict) else {}
    book = character_book if character_book is not None else native.get("characterBook")
    book_payload = to_tavern_character_book(book) if isinstance(book, dict) else None

    extensions = dict(tavern_meta.get("extensions") or {})
    extensions["nekobot"] = {
        "basicInfo": _as_text(native.get("basicInfo")),
        "rules": [_as_text(rule) for rule in _as_list(native.get("rules")) if _as_text(rule)],
        "responseFormat": _as_text(native.get("responseFormat")),
        "state": native.get("state") if isinstance(native.get("state"), dict) else {},
        "characterId": _as_text(native.get("id")),
    }

    is_v3 = str(version).lower() in {"v3", "3", "chara_card_v3"}
    data: Dict[str, Any] = {
        "name": name,
        "description": _as_text(native.get("description")),
        "personality": _as_text(native.get("personality")),
        "scenario": _as_text(native.get("scenario")),
        "first_mes": _as_text(native.get("firstMessage")),
        "mes_example": _as_text(native.get("exampleDialogues")),
        "creator_notes": _as_text(tavern_meta.get("creatorNotes")),
        "system_prompt": _as_text(native.get("systemPrompt")),
        "post_history_instructions": _as_text(tavern_meta.get("postHistoryInstructions")),
        "alternate_greetings": [
            _as_text(item) for item in _as_list(native.get("alternateGreetings"))
            if _as_text(item).strip()
        ],
        "tags": [_as_text(tag) for tag in _as_list(native.get("tags")) if _as_text(tag).strip()],
        "creator": _as_text(tavern_meta.get("creator")),
        "character_version": _as_text(tavern_meta.get("characterVersion")) or "1.0",
        "extensions": extensions,
    }
    if book_payload:
        data["character_book"] = book_payload
    if is_v3:
        data["nickname"] = _as_text(tavern_meta.get("nickname"))
        data["group_only_greetings"] = [
            _as_text(item) for item in _as_list(tavern_meta.get("groupOnlyGreetings"))
            if _as_text(item).strip()
        ]
        data["source_url"] = _as_text(tavern_meta.get("sourceUrl"))

    return {
        "spec": SPEC_V3 if is_v3 else SPEC_V2,
        "spec_version": "3.0" if is_v3 else "2.0",
        "data": data,
    }


def encode_card_payload(card: Dict[str, Any]) -> str:
    """把卡片编码为 PNG tEXt 可用的 base64 文本。"""
    raw = json.dumps(card, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def build_png_payloads(native: Dict[str, Any]) -> Dict[str, str]:
    """构建 PNG 需要写入的 tEXt 文本块（同时写入 v3 与 v2）。"""
    v3_card = to_tavern_card(native, "v3")
    v2_card = to_tavern_card(native, "v2")
    return {
        "ccv3": encode_card_payload(v3_card),
        "chara": encode_card_payload(v2_card),
    }


def build_card_png(native: Dict[str, Any], base_image: Optional[bytes] = None) -> bytes:
    """导出带内嵌卡片数据的 PNG。没有立绘时使用纯色占位图。"""
    image = base_image if is_png(base_image or b"") else build_placeholder_png()
    return insert_png_text_chunks(image, build_png_payloads(native))


def extract_greetings(native: Dict[str, Any]) -> List[str]:
    """返回全部开场白：主开场白在前，备用开场白在后（去重）。"""
    greetings: List[str] = []
    primary = _as_text(native.get("firstMessage")).strip()
    if primary:
        greetings.append(primary)
    for item in _as_list(native.get("alternateGreetings")):
        text = _as_text(item).strip()
        if text and text not in greetings:
            greetings.append(text)
    return greetings


def pick_greeting(
    native: Dict[str, Any],
    index: Optional[int] = None,
    randomize: bool = False,
) -> Tuple[str, int]:
    """挑选开场白，返回 (文本, 索引)。index=None 且 randomize=False 时取第一条。"""
    import random

    greetings = extract_greetings(native)
    if not greetings:
        return "", -1

    if index is not None:
        try:
            picked = int(index)
        except (TypeError, ValueError):
            picked = 0
        if 0 <= picked < len(greetings):
            return greetings[picked], picked
        return greetings[0], 0

    if randomize:
        picked = random.randrange(len(greetings))
        return greetings[picked], picked
    return greetings[0], 0
