"""角色卡归一化解析器

世界模拟里的角色来源很杂：手填表单、角色卡预设（custom_personality_presets）、
酒馆（SillyTavern）v1/v2/v3 卡、内嵌 character_book。这里把它们统一成
世界引擎可以直接消费的结构：

::

    {
      "name": "露娜",
      "source_type": "imported" | "manual" | "preset" | "tavern",
      "source_id": "preset_luna",
      "card": {
        "role": "钟塔守夜人",
        "goals": ["保护钟塔"],
        "personality": "冷静",
        ...
      }
    }

字段名兼容常见别名（``char_name`` / ``occupation`` / ``goals`` 之类），
未知字段原样留在 ``card["extra"]`` 里，避免导入即丢数据。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional

_log = logging.getLogger(__name__)

# 归一化字段 → 可接受的别名（按优先级）
FIELD_ALIASES: Dict[str, tuple] = {
    "name": ("name", "char_name", "character_name", "charName", "nickname"),
    "role": ("role", "occupation", "job", "position", "identity", "title"),
    "personality": ("personality", "traits", "character_personality"),
    "goals": ("goals", "goal", "motivation", "motivations", "objectives"),
    "description": ("description", "desc", "background", "bio"),
    "appearance": ("appearance", "looks", "outfit"),
    "scenario": ("scenario", "situation", "scene"),
    "first_message": ("first_message", "firstMessage", "first_mes", "greeting"),
    "speech_style": ("speech_style", "speaking_style", "tone", "voice"),
    "secrets": ("secrets", "secret", "hidden"),
    "rules": ("rules", "constraints", "taboos"),
    "aliases": ("aliases", "aka", "other_names"),
}

_CARD_KEYS = {
    alias
    for aliases in FIELD_ALIASES.values()
    for alias in aliases
}

_LIST_FIELDS = {"goals", "secrets", "rules", "aliases"}

# 酒馆卡格式：命中则走 from_tavern_card，保留内嵌 character_book 与 v2/v3 元信息
TAVERN_FORMATS = ("tavern_v1", "tavern_v2", "tavern_v3")

# 原生角色卡（custom_personality_presets）特征字段
NATIVE_MARKERS = ("basicInfo", "exampleDialogues", "systemPrompt", "responseFormat")


def _detect_format(raw: Dict[str, Any]) -> str:
    """探测卡片格式；酒馆卡模块不可用或探测异常时按未知处理，不阻断导入。"""
    try:
        from nbot.character.tavern_card import detect_card_format
    except Exception:  # pragma: no cover - 模块缺失时退回通用归一化
        _log.debug("[CharacterResolver] 酒馆卡模块不可用，跳过格式探测")
        return "unknown"
    try:
        return detect_card_format(raw)
    except Exception:  # pragma: no cover - 探测异常不应打断导入
        return "unknown"


def _unwrap_card(raw: Any) -> Dict[str, Any]:
    """解开 ``{"card": {...}}`` 外层包装；带 spec 的酒馆载荷保持原样。"""
    data = raw if isinstance(raw, dict) else {}
    nested = data.get("card")
    if not isinstance(nested, dict) or str(data.get("spec") or "").strip():
        return data
    merged = {key: value for key, value in data.items() if key != "card"}
    merged.update(nested)
    return merged


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return "、".join(_as_text(item) for item in value if _as_text(item))
    if isinstance(value, dict):
        return ""
    return str(value).strip()


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [text for text in (_as_text(item) for item in value) if text]
    text = _as_text(value)
    if not text:
        return []
    # 支持「目标1、目标2」「目标1,目标2」这类手写串
    for separator in ("、", ";", "；", "\n", ","):
        if separator in text:
            parts = [part.strip() for part in text.split(separator)]
            return [part for part in parts if part]
    return [text]


def _pick(raw: Dict[str, Any], field: str) -> Any:
    for alias in FIELD_ALIASES[field]:
        if alias in raw and raw[alias] not in (None, ""):
            return raw[alias]
    return None


class CharacterResolver:
    """把各种来源的角色卡归一化为世界角色结构。"""

    def normalize_card(
        self,
        raw: Optional[Dict[str, Any]],
        source_type: str = "manual",
        source_id: str = "",
    ) -> Dict[str, Any]:
        """归一化单张角色卡；缺少名称时抛 ValueError。"""
        data = raw if isinstance(raw, dict) else {}

        # 兼容「包了一层 card」的输入
        nested = data.get("card")
        if isinstance(nested, dict):
            data = {**data, **nested}

        name = _as_text(_pick(data, "name"))
        if not name:
            raise ValueError("角色卡缺少名称")

        card: Dict[str, Any] = {}
        for field in FIELD_ALIASES:
            if field == "name":
                continue
            value = _pick(data, field)
            if value is None:
                continue
            card[field] = _as_list(value) if field in _LIST_FIELDS else _as_text(value)

        extra = {
            key: value
            for key, value in data.items()
            if key not in _CARD_KEYS and key not in ("card", "source_type", "source_id")
        }
        if extra:
            card["extra"] = extra

        return {
            "name": name,
            "source_type": str(source_type or "manual"),
            "source_id": str(source_id or ""),
            "card": card,
        }

    def normalize_many(
        self,
        items: Optional[Iterable[Any]],
        source_type: str = "imported",
        source_id: str = "",
    ) -> List[Dict[str, Any]]:
        """批量归一化；单条失败跳过并记录日志，不阻断整批导入。"""
        results: List[Dict[str, Any]] = []
        for index, item in enumerate(items or []):
            if not isinstance(item, dict):
                continue
            try:
                results.append(
                    self.normalize_card(item, source_type=source_type, source_id=source_id)
                )
            except ValueError as exc:
                _log.warning("[CharacterResolver] 跳过第 %d 张角色卡: %s", index, exc)
        return results

    def normalize_any(
        self,
        raw: Optional[Dict[str, Any]],
        source_type: str = "imported",
        source_id: str = "",
    ) -> Dict[str, Any]:
        """单张卡：自动识别酒馆 v1-v3 / 原生预设 / 通用别名后归一化。"""
        card = _unwrap_card(raw)
        fmt = _detect_format(card)
        if fmt in TAVERN_FORMATS:
            # 酒馆卡来源明确，统一标 "tavern"；调用方显式指定自定义类型时尊重调用方
            tavern_type = source_type if source_type not in ("", "imported", "tavern") else "tavern"
            return self.from_tavern_card(
                card, source_id=source_id, source_type=tavern_type
            )
        if any(marker in card for marker in NATIVE_MARKERS):
            # 原生预设来源明确，默认标 "preset"；调用方显式指定自定义类型时尊重调用方
            preset_type = source_type if source_type not in ("", "imported", "preset") else "preset"
            return self.from_native_card(
                card, source_id=source_id, source_type=preset_type
            )
        return self.normalize_card(card, source_type=source_type, source_id=source_id)

    def normalize_payload(
        self,
        payload: Optional[Dict[str, Any]],
        source_type: str = "imported",
        source_id: str = "",
    ) -> List[Dict[str, Any]]:
        """导入入口：兼容 ``{"characters": [...]}`` 批量与单张卡（含酒馆卡）。"""
        data = payload if isinstance(payload, dict) else {}
        items = data.get("characters")
        if not isinstance(items, list):
            single = data.get("card")
            items = [single if isinstance(single, dict) else data]

        results: List[Dict[str, Any]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            try:
                results.append(
                    self.normalize_any(item, source_type=source_type, source_id=source_id)
                )
            except ValueError as exc:
                _log.warning("[CharacterResolver] 跳过第 %d 张角色卡: %s", index, exc)
        return results

    def from_native_card(
        self, native: Dict[str, Any], source_id: str = "", source_type: str = "preset"
    ) -> Dict[str, Any]:
        """从本项目原生角色卡（custom_personality_presets 格式）生成世界角色。"""
        data = native if isinstance(native, dict) else {}
        result = self.normalize_card(
            {
                "name": data.get("name"),
                "description": data.get("description") or data.get("basicInfo"),
                "personality": data.get("personality"),
                "scenario": data.get("scenario"),
                "first_message": data.get("firstMessage"),
                "speech_style": data.get("responseFormat"),
                "rules": data.get("rules") or [],
                "goals": data.get("goals") or [],
                "aliases": data.get("aliases") or [],
            },
            source_type=source_type,
            source_id=source_id or str(data.get("id") or ""),
        )

        card = result["card"]
        if _as_text(data.get("basicInfo")):
            card["basic_info"] = _as_text(data["basicInfo"])
        tags = [text for text in (_as_text(tag) for tag in data.get("tags") or []) if text]
        if tags:
            card["tags"] = tags
        if isinstance(data.get("state"), dict):
            card["state"] = dict(data["state"])
        if str(data.get("characterBookId") or "").strip():
            card["character_book_id"] = str(data["characterBookId"]).strip()
        return result

    def from_tavern_card(
        self, raw: Dict[str, Any], source_id: str = "", source_type: str = "tavern"
    ) -> Dict[str, Any]:
        """从酒馆 v1-v3 卡（或 PNG 解析出的 payload）生成世界角色。"""
        from nbot.character.tavern_card import normalize_tavern_card

        native, meta = normalize_tavern_card(raw)
        data: Dict[str, Any] = {
            "name": native.get("name", ""),
            "description": native.get("description", ""),
            "personality": native.get("personality", ""),
            "scenario": native.get("scenario", ""),
            "first_message": native.get("firstMessage", ""),
            "goals": native.get("rules") or [],
            "speech_style": native.get("responseFormat", ""),
            "tags": native.get("tags") or [],
        }
        result = self.normalize_card(data, source_type=source_type, source_id=source_id)
        card = result["card"]
        # 酒馆专属字段提到 card 顶层，方便世界角色直接消费（不再压进 extra）
        card["tavern"] = dict(meta.get("tavern") or {})
        card["character_book"] = dict(meta.get("character_book") or {})
        card["tavern_format"] = meta.get("format", "")
        card["warnings"] = list(meta.get("warnings") or [])
        greetings = [text for text in (meta.get("alternate_greetings") or []) if text]
        if greetings:
            card["alternate_greetings"] = greetings
        if _as_text(native.get("basicInfo")):
            card["basic_info"] = _as_text(native["basicInfo"])
        if _as_text(native.get("exampleDialogues")):
            card["example_dialogues"] = _as_text(native["exampleDialogues"])
        if _as_text(native.get("systemPrompt")):
            card["system_prompt"] = _as_text(native["systemPrompt"])
        if isinstance(native.get("state"), dict):
            card["state"] = dict(native["state"])
        return result
