"""Prompt Manager（提示词预设 / preset）

对标 SillyTavern 的 Prompt Manager：把"这一轮送给模型的提示词"拆成若干**有序段落**，
每段可单独开关、指定角色（system/user/assistant）、插入位置与深度，并可整体导入导出
酒馆 preset JSON。会话可以通过 ``prompt_preset_id`` 绑定某个 preset。

数据文件：``data/web/prompt_presets.json``，结构::

    {
      "presets": [
        {
          "id": "...", "name": "...", "description": "...",
          "sections": [
            {
              "id": "...", "name": "...", "order": 0, "enabled": true,
              "role": "system", "depth": 4, "position": "end",
              "content": "...", "marker": false
            }
          ],
          "tavern": {},          # 酒馆源文件里的采样等标量，导出时原样带回
          "source": "native" | "tavern",
          "created_at": "...", "updated_at": "..."
        }
      ],
      "default_id": "<id>",
      "sessions": {"<会话ID>": "<preset_id>"}
    }

段落语义：
- ``position="start"``：拼到 system 提示词**最前面**（角色卡之前）；
- ``position="end"``：拼到 system 提示词**末尾**（角色卡之后、动态运行时段之前），默认值；
- ``position="depth"``：按 ``depth`` 插入消息序列（距对话末尾第 N 条，0 = 最末尾）。

role 为 ``system`` 的 start/end 段落会进入 system 提示词；``user`` / ``assistant``
的 start/end 段落作为独立消息插在 system 之后、历史消息之前（对齐酒馆行为）。

**无 preset 时零行为变化**：只有会话显式绑定了 preset，``resolve_session_preset``
才返回内容；未绑定 / 绑定失效 / 存储异常一律返回 ``None``，管线沿用原有代码路径。
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Any

_log = logging.getLogger(__name__)

MAX_PRESETS = 100
MAX_SECTIONS = 100
MAX_NAME_LENGTH = 60
MAX_DESCRIPTION_LENGTH = 500
MAX_CONTENT_LENGTH = 20000
MAX_DEPTH = 100
DEFAULT_DEPTH = 4

VALID_ROLES = ("system", "user", "assistant")
VALID_POSITIONS = ("start", "end", "depth")

_PRESETS_KEY = "presets"
_DEFAULT_KEY = "default_id"
_SESSIONS_KEY = "sessions"
_STORE: PromptPresetStore | None = None

# 酒馆 preset 里与本项目有关的采样/上下文标量，导出时原样带回，避免往返丢失
_TAVERN_SCALAR_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "top_a",
    "min_p",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "openai_max_context",
    "openai_max_tokens",
    "seed",
    "wrap_in_quotes",
    "names_behavior",
    "send_if_empty",
    "stream_openai",
)


def _as_text(value: Any, limit: int = MAX_CONTENT_LENGTH) -> str:
    return str(value or "").strip()[:limit]


def _as_int(value: Any, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def normalize_section(raw: Any, *, index: int = 0) -> dict[str, Any] | None:
    """归一化单个段落；既无内容也不是占位标记时返回 None（视为删除/忽略）。"""
    if not isinstance(raw, dict):
        return None

    marker = _as_bool(raw.get("marker"), False)
    content = _as_text(raw.get("content"))
    if not content and not marker:
        return None

    role = str(raw.get("role") or "system").strip().lower()
    if role not in VALID_ROLES:
        role = "system"

    position = str(raw.get("position") or "end").strip().lower()
    if position not in VALID_POSITIONS:
        position = "end"

    depth = _as_int(raw.get("depth", DEFAULT_DEPTH), DEFAULT_DEPTH)
    depth = max(0, min(MAX_DEPTH, depth))

    name = _as_text(raw.get("name") or raw.get("title"), MAX_NAME_LENGTH) or f"段落 {index + 1}"
    section_id = _as_text(raw.get("id") or raw.get("identifier"), 80) or uuid.uuid4().hex[:12]

    return {
        "id": section_id,
        "name": name,
        "order": _as_int(raw.get("order", index), index),
        "enabled": _as_bool(raw.get("enabled"), True),
        "role": role,
        "depth": depth,
        "position": position,
        "content": content,
        "marker": marker,
    }


def normalize_sections(raw: Any) -> list[dict[str, Any]]:
    """归一化段落列表：按 order 排序并把 order 压成 0..N-1，id 去重。"""
    if not isinstance(raw, list):
        return []

    sections: list[dict[str, Any]] = []
    for index, item in enumerate(raw[: MAX_SECTIONS * 2]):
        section = normalize_section(item, index=index)
        if section is None:
            continue
        sections.append(section)

    indexed = list(enumerate(sections))
    indexed.sort(key=lambda pair: (pair[1]["order"], pair[0]))
    sections = [section for _, section in indexed]
    seen = set()
    for order, section in enumerate(sections):
        section["order"] = order
        base_id = section["id"]
        candidate = base_id
        suffix = 2
        while candidate in seen:
            candidate = f"{base_id}-{suffix}"
            suffix += 1
        section["id"] = candidate
        seen.add(candidate)
    return sections[:MAX_SECTIONS]


def normalize_preset(data: Any, *, preset_id: str = "") -> dict[str, Any] | None:
    """归一化 preset；非 dict 返回 None（名称由调用方校验）。"""
    if not isinstance(data, dict):
        return None

    now = datetime.now().isoformat(timespec="seconds")
    tavern = data.get("tavern")
    tavern = dict(tavern) if isinstance(tavern, dict) else {}

    return {
        "id": _as_text(preset_id or data.get("id"), 80) or uuid.uuid4().hex,
        "name": _as_text(data.get("name"), MAX_NAME_LENGTH),
        "description": _as_text(data.get("description"), MAX_DESCRIPTION_LENGTH),
        "sections": normalize_sections(data.get("sections")),
        "tavern": tavern,
        "source": "tavern" if str(data.get("source") or "") == "tavern" else "native",
        "created_at": str(data.get("created_at") or now),
        "updated_at": now,
    }


# ---------------------------------------------------------------------------
# 酒馆 preset 互转
# ---------------------------------------------------------------------------


def parse_tavern_preset(payload: Any) -> tuple[dict[str, Any], list[str]]:
    """把酒馆 preset JSON 转成本项目的 preset 结构，返回 ``(preset, warnings)``。"""
    warnings: list[str] = []
    if not isinstance(payload, dict):
        raise ValueError("preset 内容必须是 JSON 对象")

    prompts = payload.get("prompts")
    if not isinstance(prompts, list):
        raise ValueError("酒馆 preset 缺少 prompts 数组")

    # 酒馆新版把"是否启用/顺序"放在 prompt_order 里（可有多套，取第一套）
    order_entries: list[dict[str, Any]] = []
    raw_order = payload.get("prompt_order")
    if isinstance(raw_order, list):
        for block in raw_order:
            if isinstance(block, dict) and isinstance(block.get("order"), list):
                order_entries = [item for item in block["order"] if isinstance(item, dict)]
                break

    order_index: dict[str, int] = {}
    enabled_map: dict[str, bool] = {}
    for position, item in enumerate(order_entries):
        identifier = _as_text(item.get("identifier"), 80)
        if not identifier:
            continue
        order_index[identifier] = position
        enabled_map[identifier] = _as_bool(item.get("enabled"), True)
    if not order_entries:
        warnings.append("缺少 prompt_order，按 prompts 数组顺序导入")

    sections: list[dict[str, Any]] = []
    for position, item in enumerate(prompts):
        if not isinstance(item, dict):
            continue
        identifier = _as_text(item.get("identifier"), 80)
        marker = _as_bool(item.get("marker"), False)
        content = _as_text(item.get("content"))
        if not content and not marker:
            continue

        deep = _as_int(item.get("injection_position", 0), 0) == 1
        section = {
            "id": identifier or f"prompt-{position}",
            "name": _as_text(item.get("name"), MAX_NAME_LENGTH) or identifier or f"段落 {position + 1}",
            "order": order_index.get(identifier, position),
            "enabled": enabled_map.get(identifier, _as_bool(item.get("enabled"), True)),
            "role": item.get("role") or "system",
            "depth": _as_int(item.get("injection_depth", DEFAULT_DEPTH), DEFAULT_DEPTH),
            "position": "depth" if deep else "end",
            "content": content,
            "marker": marker,
        }
        sections.append(section)

    tavern: dict[str, Any] = {
        key: payload[key] for key in _TAVERN_SCALAR_KEYS if key in payload
    }
    preset = {
        "name": _as_text(payload.get("name"), MAX_NAME_LENGTH) or "导入的酒馆 preset",
        "description": _as_text(payload.get("description"), MAX_DESCRIPTION_LENGTH),
        "sections": normalize_sections(sections),
        "tavern": tavern,
        "source": "tavern",
    }
    if not preset["sections"]:
        warnings.append("prompts 里没有可用的段落内容")
    return preset, warnings


def preset_to_tavern(preset: Any) -> dict[str, Any]:
    """把本项目 preset 导出成酒馆兼容的 JSON（prompts + prompt_order）。"""
    preset = preset or {}
    sections = normalize_sections(preset.get("sections"))
    tavern = preset.get("tavern") if isinstance(preset.get("tavern"), dict) else {}

    prompts: list[dict[str, Any]] = []
    order: list[dict[str, Any]] = []
    for section in sections:
        identifier = section["id"]
        deep = section["position"] == "depth"
        prompts.append({
            "identifier": identifier,
            "name": section["name"],
            "system_prompt": section["role"] == "system" and not deep,
            "role": section["role"],
            "content": section["content"],
            "injection_position": 1 if deep else 0,
            "injection_depth": section["depth"],
            "injection_trigger": [],
            "forbid_overrides": False,
            "marker": section["marker"],
            "enabled": section["enabled"],
        })
        order.append({"identifier": identifier, "enabled": section["enabled"]})

    payload: dict[str, Any] = {
        "name": _as_text(preset.get("name"), MAX_NAME_LENGTH) or "preset",
        "temperature": 0.8,
        "top_p": 1,
        "top_k": 0,
        "openai_max_context": 200000,
        "openai_max_tokens": 8192,
        "prompts": prompts,
        "prompt_order": [{"character_id": 100000, "order": order}],
    }
    payload.update({k: v for k, v in tavern.items() if k in _TAVERN_SCALAR_KEYS})
    return payload


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def build_preset_injection(
    preset: Any, expander=None, *, expand_system: bool = True, expand_messages: bool = True
) -> dict[str, Any]:
    """把 preset 渲染成可注入的片段。

    返回::

        {
          "start": "...",                  # 拼到 system 最前
          "end": "...",                    # 拼到 system 末尾
          "top_messages": [{"role","content","id","name"}],
          "depth": [{"depth","role","content","id","name"}],
          "debug": [...],                  # 供 prompt_stack_debug
          "chars": int,
          "empty": bool,
        }

    ``expander`` 为可选 ``callable(text) -> text``，用于展开宏。
    """
    result: dict[str, Any] = {
        "start": "",
        "end": "",
        "top_messages": [],
        "depth": [],
        "debug": [],
        "chars": 0,
        "empty": True,
    }
    if not isinstance(preset, dict):
        return result

    sections = normalize_sections(preset.get("sections"))
    start_parts: list[str] = []
    end_parts: list[str] = []

    def _expand(text: str) -> str:
        if expander is None:
            return text
        try:
            return str(expander(text) or "")
        except Exception:  # pragma: no cover - 宏展开失败不影响主流程
            return text

    for section in sections:
        if not section["enabled"]:
            continue
        is_system = section["role"] == "system" and section["position"] != "depth"
        should_expand = expand_system if is_system else expand_messages
        content = (_expand(section["content"]) if should_expand else section["content"]).strip()
        if not content:
            continue
        entry = {
            "key": f"preset.{section['id']}",
            "name": section["name"],
            "role": section["role"],
            "chars": len(content),
            "preview": content[:120],
        }
        if section["position"] == "depth":
            result["depth"].append({
                "depth": section["depth"],
                "order": section["order"],
                "role": section["role"],
                "content": content,
                "id": section["id"],
                "name": section["name"],
            })
            entry["position"] = f"depth:{section['depth']}"
        elif section["role"] != "system":
            result["top_messages"].append({
                "role": section["role"],
                "content": content,
                "id": section["id"],
                "name": section["name"],
            })
            entry["position"] = section["position"]
        elif section["position"] == "start":
            start_parts.append(content)
            entry["position"] = "start"
        else:
            end_parts.append(content)
            entry["position"] = "end"

        result["debug"].append(entry)
        result["chars"] += len(content)

    result["start"] = "\n\n".join(part for part in start_parts if part)
    result["end"] = "\n\n".join(part for part in end_parts if part)
    result["empty"] = not (
        result["start"] or result["end"] or result["top_messages"] or result["depth"]
    )
    return result


def apply_preset_base(preset: Any, base_prompt: str, expander=None) -> str:
    """把 preset 的 system 段落并进 system 提示词（start 在前、end 在后）。"""
    injection = build_preset_injection(preset, expander=expander, expand_messages=False)
    if injection["empty"]:
        return base_prompt

    parts: list[str] = []
    if injection["start"]:
        parts.append(injection["start"])
    if base_prompt and base_prompt.strip():
        parts.append(base_prompt.strip())
    if injection["end"]:
        parts.append(injection["end"])
    return "\n\n".join(parts).strip() if parts else base_prompt


def apply_preset_messages(preset: Any, messages: list[dict[str, Any]], expander=None):
    """把 preset 的非 system 段落与 depth 段落插入消息序列。"""
    injection = build_preset_injection(preset, expander=expander, expand_system=False)
    return insert_preset_messages(injection, messages)


def insert_preset_messages(injection: dict[str, Any], messages: list[dict[str, Any]]):
    """按 ``build_preset_injection`` 的结果插入消息。

    - start/end 的非 system 段落作为独立消息插在 system 之后、历史之前；
    - depth 段落按"距末尾第 N 条"插入（先插深的位置，保证相互不挤位）。
    """
    if not isinstance(injection, dict) or injection.get("empty", True):
        return messages

    out = list(messages)
    top = [{"role": item["role"], "content": item["content"]} for item in injection["top_messages"]]
    if top:
        head = 1 if out and out[0].get("role") == "system" else 0
        out[head:head] = top

    base_length = len(out)
    anchored = [
        (max(1, base_length - max(0, min(MAX_DEPTH, int(item["depth"])))), item)
        for item in injection["depth"]
    ]
    anchored.sort(key=lambda pair: (pair[0], int(pair[1]["order"])))
    for offset, (index, item) in enumerate(anchored):
        out.insert(index + offset, {"role": item["role"], "content": item["content"]})
    return out


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class PromptPresetStore:
    """提示词预设存储（线程安全 + 原子写入，复用 JsonStore）。"""

    def __init__(self, base_dir: str | None = None):
        from nbot.character.storage.json_store import JsonStore

        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self._store = JsonStore(os.path.join(base_dir, "data", "web", "prompt_presets.json"))

    # ---- 读取 ----

    def list(self) -> list[dict[str, Any]]:
        raw = self._store.get(_PRESETS_KEY)
        if not isinstance(raw, list):
            return []
        result: list[dict[str, Any]] = []
        for item in raw:
            preset = normalize_preset(item, preset_id=str((item or {}).get("id") or ""))
            if preset is not None:
                result.append(preset)
        return result

    def get(self, preset_id: str) -> dict[str, Any] | None:
        preset_id = _as_text(preset_id, 80)
        if not preset_id:
            return None
        for preset in self.list():
            if preset["id"] == preset_id:
                return preset
        return None

    def default_id(self) -> str:
        return _as_text(self._store.get(_DEFAULT_KEY), 80)

    def default(self) -> dict[str, Any] | None:
        preset = self.get(self.default_id())
        if preset is not None:
            return preset
        presets = self.list()
        return presets[0] if presets else None

    # ---- 写入 ----

    def _write_presets(self, presets: list[dict[str, Any]]) -> None:
        self._store.set(_PRESETS_KEY, presets)

    def save(self, data: Any) -> dict[str, Any]:
        """新增或更新 preset，返回归一化结果；名称为空时抛 ``ValueError``。"""
        preset_id = _as_text((data or {}).get("id"), 80) if isinstance(data, dict) else ""
        preset = normalize_preset(data, preset_id=preset_id)
        if preset is None:
            raise ValueError("preset 内容必须是 JSON 对象")
        if not preset["name"]:
            raise ValueError("preset 名称不能为空")

        presets = self.list()
        replaced = False
        for index, existing in enumerate(presets):
            if existing["id"] == preset["id"]:
                preset["created_at"] = existing.get("created_at") or preset["created_at"]
                presets[index] = preset
                replaced = True
                break
        if not replaced:
            if len(presets) >= MAX_PRESETS:
                raise ValueError(f"preset 数量已达上限（{MAX_PRESETS}）")
            presets.append(preset)

        self._write_presets(presets)
        if not self.default_id():
            self._store.set(_DEFAULT_KEY, preset["id"])
        return preset

    def delete(self, preset_id: str) -> bool:
        preset_id = _as_text(preset_id, 80)
        if not preset_id:
            return False
        presets = self.list()
        remaining = [p for p in presets if p["id"] != preset_id]
        if len(remaining) == len(presets):
            return False
        self._write_presets(remaining)
        if self.default_id() == preset_id:
            if remaining:
                self._store.set(_DEFAULT_KEY, remaining[0]["id"])
            else:
                self._store.delete(_DEFAULT_KEY)

        # 清掉指向已删除 preset 的会话绑定，避免会话继续引用幽灵 preset
        sessions = self.bindings()
        stale = [sid for sid, pid in sessions.items() if pid == preset_id]
        if stale:
            for session_id in stale:
                sessions.pop(session_id, None)
            self._store.set(_SESSIONS_KEY, sessions)
        return True

    def set_default(self, preset_id: str) -> bool:
        preset_id = _as_text(preset_id, 80)
        if not preset_id:
            self._store.delete(_DEFAULT_KEY)
            return True
        if self.get(preset_id) is None:
            return False
        self._store.set(_DEFAULT_KEY, preset_id)
        return True

    def reorder(self, preset_id: str, order: Any) -> dict[str, Any] | None:
        """按给定 id 顺序重排段落；未列出的段落保持原顺序追加。"""
        preset = self.get(preset_id)
        if preset is None or not isinstance(order, list):
            return None
        wanted = [_as_text(item, 80) for item in order]
        by_id = {section["id"]: section for section in preset["sections"]}
        sections: list[dict[str, Any]] = []
        for section_id in wanted:
            section = by_id.pop(section_id, None)
            if section is not None:
                sections.append(section)
        sections.extend(section for _, section in sorted(by_id.items(), key=lambda kv: kv[1]["order"]))
        for index, section in enumerate(sections):
            section["order"] = index
        preset["sections"] = sections
        return self.save(preset)

    # ---- 会话绑定 ----

    def bindings(self) -> dict[str, str]:
        raw = self._store.get(_SESSIONS_KEY)
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): str(value)
            for key, value in raw.items()
            if str(key).strip() and str(value).strip()
        }

    def bound_preset_id(self, session_id: str) -> str:
        session_id = _as_text(session_id, 200)
        if not session_id:
            return ""
        return self.bindings().get(session_id, "")

    def bind_session(self, session_id: str, preset_id: str = "") -> bool:
        """绑定/解绑会话；preset_id 为空表示解绑，preset 不存在返回 False。"""
        session_id = _as_text(session_id, 200)
        if not session_id:
            return False
        preset_id = _as_text(preset_id, 80)
        if preset_id and self.get(preset_id) is None:
            return False

        sessions = self.bindings()
        if preset_id:
            sessions[session_id] = preset_id
        else:
            sessions.pop(session_id, None)
        self._store.set(_SESSIONS_KEY, sessions)
        return True

    # ---- 导入导出 ----

    def import_preset(self, payload: Any, mode: str = "merge") -> dict[str, Any]:
        """导入 preset；支持酒馆格式与本项目原生格式。"""
        if isinstance(payload, dict) and isinstance(payload.get("preset"), dict):
            payload = payload["preset"]
        if not isinstance(payload, dict):
            raise ValueError("导入内容必须是 JSON 对象")

        warnings: list[str] = []
        if isinstance(payload.get("prompts"), list):
            preset, warnings = parse_tavern_preset(payload)
        elif isinstance(payload.get("sections"), list):
            preset = {
                "name": _as_text(payload.get("name"), MAX_NAME_LENGTH) or "导入的 preset",
                "description": _as_text(payload.get("description"), MAX_DESCRIPTION_LENGTH),
                "sections": payload.get("sections"),
                "tavern": payload.get("tavern"),
                "source": "native",
            }
        else:
            raise ValueError("无法识别的 preset 格式（缺少 prompts 或 sections）")

        mode = "overwrite" if str(mode or "").strip().lower() == "overwrite" else "merge"
        presets = self.list()
        existing = next(
            (p for p in presets if p["name"] == preset["name"]),
            None,
        )
        result: dict[str, Any] = {"added": 0, "updated": 0, "skipped": 0, "warnings": warnings}

        if existing is not None:
            if mode == "merge":
                result["skipped"] = 1
                result["preset"] = existing
                return result
            preset["id"] = existing["id"]
            preset["created_at"] = existing.get("created_at") or preset.get("created_at")
            saved = self.save(preset)
            result["updated"] = 1
            result["preset"] = saved
            return result

        if len(presets) >= MAX_PRESETS:
            raise ValueError(f"preset 数量已达上限（{MAX_PRESETS}）")
        saved = self.save(preset)
        result["added"] = 1
        result["preset"] = saved
        return result

    def export_preset(self, preset_id: str, fmt: str = "native") -> dict[str, Any] | None:
        preset = self.get(preset_id)
        if preset is None:
            return None
        if str(fmt or "").strip().lower() in ("tavern", "st", "sillytavern"):
            return preset_to_tavern(preset)
        return {
            "version": 1,
            "name": preset["name"],
            "description": preset["description"],
            "sections": preset["sections"],
            "tavern": preset["tavern"],
            "source": preset["source"],
        }

    # ---- 默认 preset ----

    def ensure_seed(self) -> dict[str, Any] | None:
        """库为空时落一个内置默认 preset（不影响未绑定的会话行为）。"""
        presets = self.list()
        if presets:
            return None
        seed = normalize_preset({
            "name": "默认 preset",
            "description": "内置示例：主要提示 + 角色设定占位 + 深度附注占位",
            "sections": [
                {
                    "id": "main",
                    "name": "主要提示",
                    "order": 0,
                    "role": "system",
                    "position": "start",
                    "content": "以下是与用户进行的角色扮演对话，请始终保持角色设定、语气与口吻一致。",
                },
                {
                    "id": "charDescription",
                    "name": "角色设定（自动取角色卡）",
                    "order": 1,
                    "role": "system",
                    "position": "end",
                    "content": "",
                    "marker": True,
                },
                {
                    "id": "authorsNote",
                    "name": "深度附注",
                    "order": 2,
                    "role": "system",
                    "position": "depth",
                    "depth": DEFAULT_DEPTH,
                    "content": "",
                    "marker": True,
                },
            ],
        })
        saved = self.save(seed)
        return saved


def get_preset_store(base_dir: str | None = None) -> PromptPresetStore:
    """返回进程内共享的 preset 存储（首次调用时创建）。"""
    global _STORE
    if _STORE is None or base_dir is not None:
        _STORE = PromptPresetStore(base_dir)
    return _STORE


def resolve_session_preset(
    conversation_id: str,
    store: PromptPresetStore | None = None,
) -> dict[str, Any] | None:
    """解析会话绑定的 preset。

    未绑定、绑定失效或存储异常时返回 ``None``——调用方据此走原有提示词路径，
    保证"没有 preset"时行为与改动前完全一致。
    """
    session_id = _as_text(conversation_id, 200)
    if not session_id:
        return None
    try:
        preset_store = store or get_preset_store()
        preset_id = preset_store.bound_preset_id(session_id)
        if not preset_id:
            return None
        return preset_store.get(preset_id)
    except Exception as exc:  # pragma: no cover - 存储异常不该阻断对话
        _log.debug("[PromptManager] 解析会话 preset 失败: %s", exc)
        return None


def describe_preset(preset: Any) -> dict[str, Any]:
    """给 UI/调试用的 preset 概览（段落数、启用数、总字数）。"""
    preset = preset or {}
    sections = normalize_sections(preset.get("sections"))
    enabled = [s for s in sections if s["enabled"]]
    return {
        "id": preset.get("id", ""),
        "name": preset.get("name", ""),
        "sections": len(sections),
        "enabled_sections": len(enabled),
        "chars": sum(len(s["content"]) for s in enabled),
        "has_depth_sections": any(s["position"] == "depth" for s in enabled),
    }
