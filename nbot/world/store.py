"""世界模拟持久化层

一个世界 = 一份 JSON 文档，落在 ``<base_dir>/data/worlds/<world_id>.json``：

::

    {
      "version": 1,
      "world": {...},                 # 世界元信息
      "nodes": {"node_x": {...}},     # 剧情节点（树）
      "current_node_id": "node_x",
      "events": [...],                # 全局事件流，按 node_id 归属
      "characters": {"char_x": {}},
      "assets": {"asset_x": {}}
    }

写入使用「临时文件 + fsync + os.replace」原子替换，读写在进程内按文件加锁，
避免 Web 面板与后台任务同时写坏数据。
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from nbot.world.state_diff import apply_state_delta

_log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# 导入/新建时的存储侧上限：防止单请求写入巨型世界（磁盘/内存 DoS）
MAX_IMPORT_NODES = 2000
MAX_IMPORT_EVENTS = 20000
MAX_IMPORT_CHARACTERS = 500
MAX_IMPORT_ASSETS = 500
# 单个文本字段上限（标题/正文/摘要），超出截断
MAX_TEXT_CHARS = 20000
MAX_NAME_CHARS = 200

# 控制字符（保留 \t \n \r）：世界内容会原样回传给前端，先在这里挡掉不可见载荷
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """字符串清洗：去控制字符 + 截断；非字符串统一转成字符串再处理。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = _CONTROL_CHARS_RE.sub("", text)
    if limit and len(text) > limit:
        text = text[:limit]
    return text


# 各实体的文本字段清洗规格（字段名 → 字符上限）
_WORLD_TEXT_LIMITS: Dict[str, int] = {
    "name": MAX_NAME_CHARS,
    "genre": MAX_NAME_CHARS,
    "tone": MAX_NAME_CHARS,
    "description": MAX_TEXT_CHARS,
    "current_time": MAX_NAME_CHARS,
    "weather": MAX_NAME_CHARS,
    "main_plot": MAX_TEXT_CHARS,
}
_NODE_TEXT_LIMITS: Dict[str, int] = {
    "branch_name": MAX_NAME_CHARS,
    "title": MAX_NAME_CHARS,
    "location": MAX_NAME_CHARS,
    "summary": MAX_TEXT_CHARS,
    "note": MAX_TEXT_CHARS,
}
_EVENT_TEXT_LIMITS: Dict[str, int] = {
    "title": MAX_NAME_CHARS,
    "content": MAX_TEXT_CHARS,
    "actor_name": MAX_NAME_CHARS,
    "actor_id": MAX_NAME_CHARS,
}
_CHARACTER_TEXT_LIMITS: Dict[str, int] = {
    "name": MAX_NAME_CHARS,
    "source_type": MAX_NAME_CHARS,
    "source_id": MAX_NAME_CHARS,
}
_CARD_TEXT_LIMITS: Dict[str, int] = {
    "role": MAX_NAME_CHARS,
    "personality": MAX_TEXT_CHARS,
    "description": MAX_TEXT_CHARS,
    "scenario": MAX_TEXT_CHARS,
    "first_message": MAX_TEXT_CHARS,
    "speech_style": MAX_TEXT_CHARS,
    "basic_info": MAX_TEXT_CHARS,
}
_ASSET_TEXT_LIMITS: Dict[str, int] = {
    "name": MAX_NAME_CHARS,
    "asset_type": MAX_NAME_CHARS,
    "description": MAX_TEXT_CHARS,
}


NODE_EVENT_TYPES = (
    "player_action",
    "narration",
    "dialogue",
    "world_event",
    "state_change",
    "system_note",
    "branch",
)

_WORLD_DEFAULT_FIELDS: Dict[str, Any] = {
    "genre": "",
    "tone": "",
    "description": "",
    "current_time": "",
    "weather": "",
    "tension": 0,
    "main_plot": "",
    "time_mode": "real",
    "time_scale": 1,
}

# 允许通过 update_world / update_node 修改的字段（其余忽略，避免脏数据）
_WORLD_EDITABLE = {
    "name",
    "genre",
    "tone",
    "description",
    "current_time",
    "weather",
    "tension",
    "main_plot",
    "time_mode",
    "time_scale",
    "cover_url",
    "tags",
    "characters_hint",
}

_NODE_EDITABLE = {"branch_name", "title", "summary", "summary_source", "tags", "location", "note"}

_FILE_LOCKS: Dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def _as_number(value: Any, default: float = 0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _as_int(value: Any, default: int = 0) -> int:
    return int(_as_number(value, default))


def _file_lock(path: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        return _FILE_LOCKS.setdefault(os.path.abspath(path), threading.Lock())


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        _log.warning("[WorldStore] 读取失败 %s: %s", path, exc)
        return {}


def _write_json(path: str, payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = (
        f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


class WorldStore:
    """世界模拟数据存储（世界 / 节点 / 事件 / 角色 / 资产）"""

    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.worlds_dir = os.path.join(base_dir, "data", "worlds")
        os.makedirs(self.worlds_dir, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 底层读写
    # ------------------------------------------------------------------ #

    def _path(self, world_id: str) -> str:
        world_id = str(world_id or "")
        if not _ID_RE.match(world_id):
            raise KeyError(world_id)
        return os.path.join(self.worlds_dir, f"{world_id}.json")

    def _load(self, world_id: str) -> Dict[str, Any]:
        raw = _read_json(self._path(world_id))
        if not raw:
            raise KeyError(world_id)
        raw.setdefault("nodes", {})
        raw.setdefault("events", [])
        raw.setdefault("characters", {})
        raw.setdefault("assets", {})
        return raw

    def _save(self, world_id: str, raw: Dict[str, Any]) -> None:
        path = self._path(world_id)
        with _file_lock(path):
            _write_json(path, raw)

    def _world(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        world = raw.get("world")
        return world if isinstance(world, dict) else {}

    @staticmethod
    def _ordered_nodes(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
        nodes = [node for node in raw.get("nodes", {}).values() if isinstance(node, dict)]
        nodes.sort(key=lambda item: (item.get("order", 0), item.get("created_at", "")))
        return nodes

    def _bundle(
        self,
        raw: Dict[str, Any],
        *,
        event_limit: Optional[int] = None,
        before_sequence: Optional[int] = None,
    ) -> Dict[str, Any]:
        nodes = self._ordered_nodes(raw)
        current_id = raw.get("current_node_id", "")
        current = next((node for node in nodes if node.get("node_id") == current_id), None)

        events = [item for item in raw.get("events", []) if isinstance(item, dict)]
        event_total = len(events)
        if before_sequence is not None:
            events = [
                item for item in events if int(item.get("sequence") or 0) < int(before_sequence)
            ]
        has_more = False
        if event_limit is not None and event_limit > 0 and len(events) > event_limit:
            events = events[-event_limit:]
            has_more = True

        return {
            "world": copy.deepcopy(self._world(raw)),
            "current_node": copy.deepcopy(current) if current else {},
            "nodes": copy.deepcopy(nodes),
            "events": copy.deepcopy(events),
            "event_total": event_total,
            "has_more": has_more,
            "characters": copy.deepcopy(list(raw.get("characters", {}).values())),
            "assets": copy.deepcopy(list(raw.get("assets", {}).values())),
        }

    # ------------------------------------------------------------------ #
    # 世界 CRUD
    # ------------------------------------------------------------------ #

    def list_worlds(self, include_archived: bool = False) -> List[Dict[str, Any]]:
        """列出所有世界（默认不含已归档），附带统计信息。"""
        worlds: List[Dict[str, Any]] = []
        try:
            names = sorted(os.listdir(self.worlds_dir))
        except OSError:
            return worlds

        for name in names:
            if not name.endswith(".json"):
                continue
            raw = _read_json(os.path.join(self.worlds_dir, name))
            world = self._world(raw)
            if not world:
                continue
            if world.get("archived") and not include_archived:
                continue
            world = copy.deepcopy(world)
            world["node_count"] = len(raw.get("nodes", {}))
            world["event_count"] = len(raw.get("events", []))
            world["character_count"] = len(raw.get("characters", {}))
            world["asset_count"] = len(raw.get("assets", {}))
            worlds.append(world)

        worlds.sort(key=lambda item: item.get("updated_at", ""), reverse=True)
        return worlds

    def create_world(self, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """创建世界，返回 {world, current_node, ...}。"""
        payload = dict(data or {})
        name = clean_text(payload.get("name"), MAX_NAME_CHARS).strip()
        if not name:
            raise ValueError("世界名称不能为空")

        now = _now()
        world: Dict[str, Any] = {
            "world_id": _new_id("world_"),
            "name": name,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        for key, default in _WORLD_DEFAULT_FIELDS.items():
            world[key] = copy.deepcopy(default)

        for key, value in payload.items():
            if key in ("world_id", "archived", "created_at", "updated_at"):
                continue
            if key in ("nodes", "events", "characters", "assets", "current_node"):
                continue
            world[key] = copy.deepcopy(value)

        world["tension"] = _as_int(world.get("tension"), 0)
        world["time_scale"] = max(1, _as_int(world.get("time_scale"), 1))
        world["time_mode"] = str(world.get("time_mode") or "real")
        self._scrub_text_fields(world, _WORLD_TEXT_LIMITS)

        branch_name = str(payload.get("branch_name") or "主线")
        node = self._build_node(world, order=0, branch_name=branch_name)
        raw = {
            "version": 1,
            "world": world,
            "nodes": {node["node_id"]: node},
            "current_node_id": node["node_id"],
            "events": [],
            "characters": {},
            "assets": {},
        }
        self._save(world["world_id"], raw)
        return self._bundle(raw)

    def _build_node(
        self,
        world: Dict[str, Any],
        *,
        order: int,
        branch_name: str,
        parent_node_id: str = "",
        state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = _now()
        if state is None:
            state = {
                "global_state": {
                    "weather": world.get("weather", ""),
                    "tension": world.get("tension", 0),
                    "main_plot": world.get("main_plot", ""),
                    "location": "",
                },
                "characters": {},
                "world_time": {
                    "display": world.get("current_time", ""),
                    "elapsed_seconds_total": 0,
                    "time_mode": world.get("time_mode", "real"),
                    "time_scale": world.get("time_scale", 1),
                },
            }
        return {
            "node_id": _new_id("node_"),
            "world_id": world.get("world_id", ""),
            "parent_node_id": parent_node_id,
            "branch_name": branch_name,
            "title": branch_name,
            "order": order,
            "state": copy.deepcopy(state),
            "summary": "",
            "location": "",
            "created_at": now,
            "updated_at": now,
        }

    def get_world(self, world_id: str) -> Optional[Dict[str, Any]]:
        try:
            return copy.deepcopy(self._world(self._load(world_id)))
        except KeyError:
            return None

    def get_world_bundle(
        self,
        world_id: str,
        *,
        event_limit: Optional[int] = None,
        before_sequence: Optional[int] = None,
    ) -> Dict[str, Any]:
        """读取世界完整数据；世界不存在时抛 KeyError。

        Args:
            event_limit: 只返回最近 N 条事件（长局分页/懒加载）
            before_sequence: 只返回 ``sequence`` 小于该值的事件（翻页游标）
        """
        return self._bundle(
            self._load(world_id),
            event_limit=event_limit,
            before_sequence=before_sequence,
        )

    def update_world(self, world_id: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        raw = self._load(world_id)
        world = self._world(raw)
        updates = dict(data or {})
        if "name" in updates:
            name = str(updates.get("name") or "").strip()
            if not name:
                raise ValueError("世界名称不能为空")
            world["name"] = name
        for key in _WORLD_EDITABLE:
            if key in updates and key != "name":
                world[key] = copy.deepcopy(updates[key])
        if "tension" in updates:
            world["tension"] = _as_int(world.get("tension"), 0)
        if "time_scale" in updates:
            world["time_scale"] = max(1, _as_int(world.get("time_scale"), 1))
        self._scrub_text_fields(world, _WORLD_TEXT_LIMITS)
        world["updated_at"] = _now()
        raw["world"] = world
        self._save(world_id, raw)
        return self._bundle(raw)

    def archive_world(self, world_id: str) -> Dict[str, Any]:
        """软删除：标记 archived（DELETE /api/worlds/<id> 走这里）。"""
        raw = self._load(world_id)
        world = self._world(raw)
        world["archived"] = True
        world["updated_at"] = _now()
        raw["world"] = world
        self._save(world_id, raw)
        return self._bundle(raw)

    def duplicate_world(self, world_id: str) -> Dict[str, Any]:
        exported = self.export_world(world_id)
        exported["world"]["name"] = f"{exported['world'].get('name', '世界')} 副本"
        return self.import_world(exported)

    # ------------------------------------------------------------------ #
    # 节点
    # ------------------------------------------------------------------ #

    def get_tree(self, world_id: str) -> List[Dict[str, Any]]:
        return copy.deepcopy(self._ordered_nodes(self._load(world_id)))

    def get_node(self, world_id: str, node_id: str) -> Optional[Dict[str, Any]]:
        raw = self._load(world_id)
        node = raw.get("nodes", {}).get(str(node_id))
        return copy.deepcopy(node) if isinstance(node, dict) else None

    def update_node(
        self, world_id: str, node_id: str, updates: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        raw = self._load(world_id)
        nodes = raw.get("nodes", {})
        node = nodes.get(str(node_id))
        if not isinstance(node, dict):
            raise KeyError(node_id)

        payload = dict(updates or {})
        for key in _NODE_EDITABLE:
            if key in payload:
                node[key] = copy.deepcopy(payload[key])
        self._scrub_text_fields(node, _NODE_TEXT_LIMITS)
        if isinstance(payload.get("state_delta"), dict):
            node["state"] = apply_state_delta(node.get("state", {}), payload["state_delta"])
        elif isinstance(payload.get("state"), dict):
            node["state"] = copy.deepcopy(payload["state"])

        node["updated_at"] = _now()
        nodes[str(node_id)] = node
        raw["nodes"] = nodes
        world = self._world(raw)
        world["updated_at"] = node["updated_at"]
        raw["world"] = world
        self._save(world_id, raw)
        return copy.deepcopy(node)

    def delete_node(self, world_id: str, node_id: str) -> Dict[str, Any]:
        raw = self._load(world_id)
        nodes = raw.get("nodes", {})
        node_id = str(node_id)
        node = nodes.get(node_id)
        if not isinstance(node, dict):
            raise KeyError(node_id)
        if not node.get("parent_node_id"):
            raise ValueError("不能删除主节点")

        parent_id = node.get("parent_node_id", "")
        del nodes[node_id]
        raw["nodes"] = nodes
        raw["events"] = [
            event for event in raw.get("events", []) if event.get("node_id") != node_id
        ]
        if raw.get("current_node_id") == node_id:
            raw["current_node_id"] = parent_id
        world = self._world(raw)
        world["updated_at"] = _now()
        raw["world"] = world
        self._save(world_id, raw)
        return self._bundle(raw)

    def activate_node(self, world_id: str, node_id: str) -> Dict[str, Any]:
        raw = self._load(world_id)
        if str(node_id) not in raw.get("nodes", {}):
            raise KeyError(node_id)
        raw["current_node_id"] = str(node_id)
        self._save(world_id, raw)
        return self._bundle(raw)

    def branch_from_event(
        self, world_id: str, event_id: str, branch_name: str = ""
    ) -> Dict[str, Any]:
        """从某条事件所在的节点派生一个独立分支节点，并切换过去。"""
        raw = self._load(world_id)
        events = raw.get("events", [])
        event = next((item for item in events if item.get("event_id") == event_id), None)
        if event is None:
            raise KeyError(event_id)

        nodes = raw.get("nodes", {})
        parent = nodes.get(str(event.get("node_id")))
        if not isinstance(parent, dict):
            raise KeyError(event.get("node_id"))

        name = str(branch_name or "").strip() or f"{parent.get('branch_name') or '主线'}·分支"
        node = self._build_node(
            self._world(raw),
            order=len(nodes),
            branch_name=name,
            parent_node_id=parent["node_id"],
            state=parent.get("state", {}),
        )
        nodes[node["node_id"]] = node
        raw["nodes"] = nodes
        raw["current_node_id"] = node["node_id"]
        branch_event = {
            "event_id": _new_id("evt_"),
            "world_id": world_id,
            "node_id": node["node_id"],
            "type": "branch",
            "title": f"分支：{name}",
            "content": f"从事件「{event.get('content', '')[:40]}」派生出分支「{name}」。",
            "state_delta": {},
            "sequence": len(events),
            "created_at": _now(),
        }
        events.append(branch_event)
        raw["events"] = events
        self._save(world_id, raw)
        return self._bundle(raw)

    def summarize_node(self, world_id: str, node_id: str) -> Dict[str, Any]:
        """生成（规则版）节点摘要，返回带 summary 的节点。"""
        raw = self._load(world_id)
        nodes = raw.get("nodes", {})
        node = nodes.get(str(node_id))
        if not isinstance(node, dict):
            raise KeyError(node_id)

        events = [item for item in raw.get("events", []) if item.get("node_id") == node["node_id"]]
        title = node.get("title") or node.get("branch_name") or "本节点"
        if events:
            highlights = [
                str(item.get("content") or item.get("title") or "").strip()[:60]
                for item in events
            ]
            highlights = [text for text in highlights if text]
            joined = "；".join(highlights[:5])
            node["summary"] = f"「{title}」累计 {len(events)} 个事件：{joined}。"
        else:
            node["summary"] = f"「{title}」暂无事件记录。"

        node["summary_source"] = "rule"
        node["updated_at"] = _now()
        nodes[node["node_id"]] = node
        raw["nodes"] = nodes
        self._save(world_id, raw)
        return copy.deepcopy(node)

    # ------------------------------------------------------------------ #
    # 事件
    # ------------------------------------------------------------------ #

    def add_event(
        self,
        world_id: str,
        node_id: str,
        event_type: str,
        content: str,
        *,
        title: str = "",
        state_delta: Optional[Dict[str, Any]] = None,
        actor_id: str = "",
        actor_name: str = "",
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """追加事件；带 state_delta 时同时推进节点状态。"""
        raw = self._load(world_id)
        nodes = raw.get("nodes", {})
        node = nodes.get(str(node_id))
        if not isinstance(node, dict):
            raise KeyError(node_id)

        kind = str(event_type or "").strip() or "world_event"
        if kind not in NODE_EVENT_TYPES:
            kind = "world_event"

        events = raw.get("events", [])
        now = _now()
        event: Dict[str, Any] = {
            "event_id": _new_id("evt_"),
            "world_id": world_id,
            "node_id": node["node_id"],
            "type": kind,
            "title": clean_text(title, MAX_NAME_CHARS),
            "content": clean_text(content),
            "state_delta": copy.deepcopy(state_delta) if isinstance(state_delta, dict) else {},
            "actor_id": str(actor_id or ""),
            "actor_name": str(actor_name or ""),
            "sequence": len(events),
            "created_at": now,
        }
        if extra:
            event.update(copy.deepcopy(extra))

        if isinstance(state_delta, dict) and state_delta:
            node["state"] = apply_state_delta(node.get("state", {}), state_delta)
        node["updated_at"] = now
        nodes[node["node_id"]] = node
        raw["nodes"] = nodes

        events.append(event)
        raw["events"] = events
        world = self._world(raw)
        world["updated_at"] = now
        raw["world"] = world
        self._save(world_id, raw)
        return copy.deepcopy(event)

    def list_events(self, world_id: str, node_id: str = "") -> List[Dict[str, Any]]:
        events = self._load(world_id).get("events", [])
        if node_id:
            events = [item for item in events if item.get("node_id") == node_id]
        return copy.deepcopy(events)

    # ------------------------------------------------------------------ #
    # 角色
    # ------------------------------------------------------------------ #

    def list_characters(self, world_id: str) -> List[Dict[str, Any]]:
        characters = self._load(world_id).get("characters", {})
        return copy.deepcopy([item for item in characters.values() if isinstance(item, dict)])

    def upsert_character(
        self, world_id: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        raw = self._load(world_id)
        characters = raw.get("characters", {})
        payload = dict(data or {})
        name = clean_text(payload.get("name"), MAX_NAME_CHARS).strip()
        if not name:
            raise ValueError("角色名称不能为空")

        character_id = str(payload.get("character_id") or "").strip()
        now = _now()
        existing = characters.get(character_id) if character_id else None
        if not character_id or (character_id and not isinstance(existing, dict)):
            character_id = character_id or _new_id("char_")
            existing = {
                "character_id": character_id,
                "created_at": now,
                "card": {},
                "source_type": "manual",
                "source_id": "",
            }

        character = dict(existing)
        character["character_id"] = character_id
        character["name"] = name
        for key, value in payload.items():
            if key in ("character_id", "created_at"):
                continue
            character[key] = copy.deepcopy(value)
        character.setdefault("card", {})
        self._scrub_text_fields(character, _CHARACTER_TEXT_LIMITS)
        if isinstance(character.get("card"), dict):
            self._scrub_text_fields(character["card"], _CARD_TEXT_LIMITS)
        character["updated_at"] = now

        characters[character_id] = character
        raw["characters"] = characters
        self._ensure_character_state(raw, character)
        self._save(world_id, raw)
        return copy.deepcopy(character)

    @staticmethod
    def _ensure_character_state(raw: Dict[str, Any], character: Dict[str, Any]) -> None:
        """在当前节点 state.characters 里为该角色建固定字段骨架。

        前端要用它渲染角色状态卡，所以字段固定为
        ``{name, mood, affection, trust, memory[]}``；模型后续回合通过
        ``memory_append`` 等增量往里面写。
        """
        nodes = raw.get("nodes", {})
        node = nodes.get(raw.get("current_node_id", ""))
        if not isinstance(node, dict):
            return
        state = node.get("state") if isinstance(node.get("state"), dict) else {}
        bucket = state.get("characters")
        if not isinstance(bucket, dict):
            bucket = {}

        character_id = str(character.get("character_id") or "")
        if not character_id:
            return

        entry = bucket.get(character_id)
        if not isinstance(entry, dict):
            entry = {"mood": "", "affection": 50, "trust": 50, "memory": []}
        entry["name"] = character.get("name", entry.get("name", ""))
        entry.setdefault("mood", "")
        entry.setdefault("affection", 50)
        entry.setdefault("trust", 50)
        if not isinstance(entry.get("memory"), list):
            entry["memory"] = []

        bucket[character_id] = entry
        state["characters"] = bucket
        node["state"] = state
        nodes[node["node_id"]] = node
        raw["nodes"] = nodes

    def delete_character(self, world_id: str, character_id: str) -> bool:
        raw = self._load(world_id)
        characters = raw.get("characters", {})
        character_id = str(character_id)
        if character_id not in characters:
            raise KeyError(character_id)
        del characters[character_id]
        raw["characters"] = characters
        self._save(world_id, raw)
        return True

    # ------------------------------------------------------------------ #
    # 资产
    # ------------------------------------------------------------------ #

    def list_assets(self, world_id: str, asset_type: str = "") -> List[Dict[str, Any]]:
        assets = self._load(world_id).get("assets", {})
        items = [item for item in assets.values() if isinstance(item, dict)]
        if asset_type:
            items = [item for item in items if item.get("asset_type") == asset_type]
        return copy.deepcopy(items)

    def upsert_asset(
        self, world_id: str, data: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        raw = self._load(world_id)
        assets = raw.get("assets", {})
        payload = dict(data or {})
        name = clean_text(payload.get("name"), MAX_NAME_CHARS).strip()
        if not name:
            raise ValueError("资产名称不能为空")

        asset_id = str(payload.get("asset_id") or "").strip()
        now = _now()
        existing = assets.get(asset_id) if asset_id else None
        if not asset_id or (asset_id and not isinstance(existing, dict)):
            asset_id = asset_id or _new_id("asset_")
            existing = {
                "asset_id": asset_id,
                "asset_type": "location",
                "description": "",
                "created_at": now,
            }

        asset = dict(existing)
        asset["asset_id"] = asset_id
        asset["name"] = name
        for key, value in payload.items():
            if key in ("asset_id", "created_at"):
                continue
            asset[key] = copy.deepcopy(value)
        self._scrub_text_fields(asset, _ASSET_TEXT_LIMITS)
        asset["updated_at"] = now

        assets[asset_id] = asset
        raw["assets"] = assets
        self._save(world_id, raw)
        return copy.deepcopy(asset)

    def delete_asset(self, world_id: str, asset_id: str) -> bool:
        raw = self._load(world_id)
        assets = raw.get("assets", {})
        asset_id = str(asset_id)
        if asset_id not in assets:
            raise KeyError(asset_id)
        del assets[asset_id]
        raw["assets"] = assets
        self._save(world_id, raw)
        return True

    # ------------------------------------------------------------------ #
    # 导入 / 导出
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 存储侧安全限制（导入体积 / 文本清洗）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _check_import_limits(
        node_count: int, event_count: int, character_count: int, asset_count: int
    ) -> None:
        """导入条数上限校验，超限抛 ValueError（路由层转 400）。"""
        checks = (
            ("节点", node_count, MAX_IMPORT_NODES),
            ("事件", event_count, MAX_IMPORT_EVENTS),
            ("角色", character_count, MAX_IMPORT_CHARACTERS),
            ("资产", asset_count, MAX_IMPORT_ASSETS),
        )
        for label, count, limit in checks:
            if count > limit:
                raise ValueError(f"导入数据过大：{label} {count} 条，超过上限 {limit} 条")

    @staticmethod
    def _scrub_text_fields(payload: Dict[str, Any], limits: Dict[str, int]) -> Dict[str, Any]:
        """按规格清洗文本字段（去控制字符 + 截断），只处理字符串值。"""
        if not isinstance(payload, dict):
            return payload
        for key, limit in limits.items():
            if isinstance(payload.get(key), str):
                payload[key] = clean_text(payload[key], limit)
        return payload

    def export_world(self, world_id: str) -> Dict[str, Any]:
        """导出为可传输的 JSON（导入时整体重建 id）。"""
        bundle = self.get_world_bundle(world_id)
        return {
            "version": 1,
            "exported_at": _now(),
            "world": bundle["world"],
            "current_node": bundle["current_node"],
            "nodes": bundle["nodes"],
            "events": bundle["events"],
            "characters": bundle["characters"],
            "assets": bundle["assets"],
        }

    def import_world(self, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """导入世界：重建全部 id，返回 {world, current_node, ..., counts}。

        存储侧硬限制：节点/事件/角色/资产条数超限直接 ValueError（路由转 400），
        文本字段做控制字符过滤与长度截断，避免单个请求写入巨型或带不可见载荷的世界。
        """
        data = dict(payload or {})
        if isinstance(data.get("export"), dict):
            data = dict(data["export"])

        nodes_in = data.get("nodes") if isinstance(data.get("nodes"), list) else []
        events_in = data.get("events") if isinstance(data.get("events"), list) else []
        characters_in = (
            data.get("characters") if isinstance(data.get("characters"), list) else []
        )
        assets_in = data.get("assets") if isinstance(data.get("assets"), list) else []
        self._check_import_limits(
            len(nodes_in), len(events_in), len(characters_in), len(assets_in)
        )

        world_data = data.get("world") if isinstance(data.get("world"), dict) else {}
        name = clean_text(world_data.get("name"), MAX_NAME_CHARS).strip()
        if not name:
            raise ValueError("导入数据缺少世界名称")

        now = _now()
        world: Dict[str, Any] = {
            "world_id": _new_id("world_"),
            "name": name,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        for key, default in _WORLD_DEFAULT_FIELDS.items():
            world[key] = copy.deepcopy(world_data.get(key, default))
        for key, value in world_data.items():
            if key in world or key in ("world_id", "created_at", "updated_at", "archived"):
                continue
            world[key] = clean_text(value) if isinstance(value, str) else copy.deepcopy(value)
        world["tension"] = _as_int(world.get("tension"), 0)
        world["time_scale"] = max(1, _as_int(world.get("time_scale"), 1))
        world["time_mode"] = str(world.get("time_mode") or "real")
        self._scrub_text_fields(world, _WORLD_TEXT_LIMITS)

        old_current = ""
        current_payload = data.get("current_node")
        if isinstance(current_payload, dict):
            old_current = str(current_payload.get("node_id") or "")
        old_current = old_current or str(data.get("current_node_id") or "")

        nodes_out: Dict[str, Any] = {}
        id_map: Dict[str, str] = {}
        for order, node in enumerate(nodes_in):
            if not isinstance(node, dict):
                continue
            new_id = _new_id("node_")
            id_map[str(node.get("node_id") or "")] = new_id
            clone = copy.deepcopy(node)
            clone["node_id"] = new_id
            clone["world_id"] = world["world_id"]
            clone["order"] = order
            clone["created_at"] = clone.get("created_at") or now
            clone["updated_at"] = now
            clone["state"] = clone.get("state") if isinstance(clone.get("state"), dict) else {}
            self._scrub_text_fields(clone, _NODE_TEXT_LIMITS)
            nodes_out[new_id] = clone

        for node in nodes_out.values():
            node["parent_node_id"] = id_map.get(str(node.get("parent_node_id") or ""), "")

        current_node_id = id_map.get(old_current, "")
        if not current_node_id:
            first = next(
                (node for node in self._ordered_nodes({"nodes": nodes_out}) if not node.get(
                    "parent_node_id"
                )),
                None,
            )
            current_node_id = first["node_id"] if first else _new_id("node_")

        if not nodes_out:
            root = self._build_node(world, order=0, branch_name="主线")
            nodes_out[root["node_id"]] = root
            current_node_id = root["node_id"]

        events_out: List[Dict[str, Any]] = []
        for sequence, event in enumerate(events_in):
            if not isinstance(event, dict):
                continue
            clone = copy.deepcopy(event)
            clone["event_id"] = _new_id("evt_")
            clone["world_id"] = world["world_id"]
            clone["node_id"] = id_map.get(str(event.get("node_id") or ""), current_node_id)
            clone["sequence"] = sequence
            self._scrub_text_fields(clone, _EVENT_TEXT_LIMITS)
            events_out.append(clone)

        characters_out: Dict[str, Any] = {}
        for character in characters_in:
            if not isinstance(character, dict):
                continue
            clone = copy.deepcopy(character)
            clone["character_id"] = _new_id("char_")
            clone["created_at"] = clone.get("created_at") or now
            clone["updated_at"] = now
            self._scrub_text_fields(clone, _CHARACTER_TEXT_LIMITS)
            if isinstance(clone.get("card"), dict):
                self._scrub_text_fields(clone["card"], _CARD_TEXT_LIMITS)
            characters_out[clone["character_id"]] = clone

        assets_out: Dict[str, Any] = {}
        for asset in assets_in:
            if not isinstance(asset, dict):
                continue
            clone = copy.deepcopy(asset)
            clone["asset_id"] = _new_id("asset_")
            clone["created_at"] = clone.get("created_at") or now
            clone["updated_at"] = now
            self._scrub_text_fields(clone, _ASSET_TEXT_LIMITS)
            assets_out[clone["asset_id"]] = clone

        raw = {
            "version": 1,
            "world": world,
            "nodes": nodes_out,
            "current_node_id": current_node_id,
            "events": events_out,
            "characters": characters_out,
            "assets": assets_out,
        }
        self._save(world["world_id"], raw)
        bundle = self._bundle(raw)
        bundle["counts"] = {
            "node_count": len(nodes_out),
            "event_count": len(events_out),
            "character_count": len(characters_out),
            "asset_count": len(assets_out),
        }
        return bundle
