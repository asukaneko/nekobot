"""用户人设（persona）库：描述"对话里的我"是谁。

与角色卡（对方是谁）互补：一个用户人设包含名称、代词与背景设定，
会话可绑定某个人设；未绑定时回退到全局默认人设。

数据文件：``data/web/personas.json``，结构
``{"personas": [ {...}, ... ], "default_id": "<id>"}``。

注入方式：命中人设后，``build_persona_block`` 生成一段说明追加到 system
prompt 末尾（在宏展开之后），并把会话的 ``{{user}}`` 默认名称设为人设名。
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

MAX_PERSONAS = 50
MAX_NAME_LENGTH = 40
MAX_PRONOUN_LENGTH = 20
MAX_DESCRIPTION_LENGTH = 2000
PROMPT_HEADER = "【用户设定】"

_PERSONAS_KEY = "personas"
_DEFAULT_KEY = "default_id"
_STORE: Optional["PersonaStore"] = None


def _as_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def normalize_persona(data: Any, *, persona_id: str = "") -> Optional[Dict[str, Any]]:
    """归一化人设；名称与设定都为空时返回 None（视为删除/忽略）。"""
    if not isinstance(data, dict):
        return None

    name = _as_text(data.get("name"), MAX_NAME_LENGTH)
    description = _as_text(data.get("description"), MAX_DESCRIPTION_LENGTH)
    pronouns = _as_text(data.get("pronouns"), MAX_PRONOUN_LENGTH)
    if not name and not description:
        return None
    if not name:
        name = "我"

    now = datetime.now().isoformat(timespec="seconds")
    return {
        "id": str(persona_id or data.get("id") or "").strip() or uuid.uuid4().hex,
        "name": name,
        "pronouns": pronouns,
        "description": description,
        "created_at": str(data.get("created_at") or now),
        "updated_at": now,
    }


def build_persona_block(persona: Any) -> str:
    """把人设渲染成注入 system prompt 的文本块；空人设返回空串。"""
    if not isinstance(persona, dict):
        return ""
    name = _as_text(persona.get("name"), MAX_NAME_LENGTH)
    pronouns = _as_text(persona.get("pronouns"), MAX_PRONOUN_LENGTH)
    description = _as_text(persona.get("description"), MAX_DESCRIPTION_LENGTH)
    if not (name or description):
        return ""

    lines = [PROMPT_HEADER]
    if name:
        lines.append(f"- 用户名称：{name}（指对话中的用户本人，不是角色）")
    if pronouns:
        lines.append(f"- 用户代词：{pronouns}")
    if description:
        lines.append(f"- 用户设定：{description}")
    lines.append("请始终按上述用户设定理解用户，不要把它当成本轮扮演的角色。")
    return "\n".join(lines)


class PersonaStore:
    """用户人设存储（线程安全 + 原子写入）。"""

    def __init__(self, base_dir: Optional[str] = None):
        from nbot.character.storage.json_store import JsonStore

        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self._store = JsonStore(os.path.join(base_dir, "data", "web", "personas.json"))

    # ---- 读取 ----

    def list(self) -> List[Dict[str, Any]]:
        raw = self._store.get(_PERSONAS_KEY)
        if not isinstance(raw, list):
            return []
        result: List[Dict[str, Any]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            persona_id = str(item.get("id") or "").strip()
            if not persona_id:
                continue
            result.append({
                "id": persona_id,
                "name": _as_text(item.get("name"), MAX_NAME_LENGTH) or "我",
                "pronouns": _as_text(item.get("pronouns"), MAX_PRONOUN_LENGTH),
                "description": _as_text(item.get("description"), MAX_DESCRIPTION_LENGTH),
                "created_at": str(item.get("created_at") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            })
        return result

    def get(self, persona_id: str) -> Optional[Dict[str, Any]]:
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            return None
        for persona in self.list():
            if persona["id"] == persona_id:
                return persona
        return None

    def default_id(self) -> str:
        return str(self._store.get(_DEFAULT_KEY) or "").strip()

    def default(self) -> Optional[Dict[str, Any]]:
        persona = self.get(self.default_id())
        if persona is not None:
            return persona
        personas = self.list()
        return personas[0] if personas else None

    def resolve(self, persona_id: str = "") -> Optional[Dict[str, Any]]:
        """按 id 解析人设；id 为空/失效时回退全局默认。"""
        return self.get(persona_id) or self.default()

    # ---- 写入 ----

    def save(self, data: Any) -> Dict[str, Any]:
        """新增或更新人设，返回归一化后的结果。"""
        persona_id = str((data or {}).get("id") or "").strip() if isinstance(data, dict) else ""
        persona = normalize_persona(data, persona_id=persona_id)
        if persona is None:
            raise ValueError("人设名称与设定不能同时为空")

        personas = self.list()
        replaced = False
        for index, existing in enumerate(personas):
            if existing["id"] == persona["id"]:
                persona["created_at"] = existing.get("created_at") or persona["created_at"]
                personas[index] = persona
                replaced = True
                break
        if not replaced:
            if len(personas) >= MAX_PERSONAS:
                raise ValueError(f"人设数量已达上限（{MAX_PERSONAS}）")
            personas.append(persona)

        self._store.set(_PERSONAS_KEY, personas)
        if not self.default_id():
            self._store.set(_DEFAULT_KEY, persona["id"])
        return persona

    def delete(self, persona_id: str) -> bool:
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            return False
        personas = self.list()
        remaining = [p for p in personas if p["id"] != persona_id]
        if len(remaining) == len(personas):
            return False
        self._store.set(_PERSONAS_KEY, remaining)
        if self.default_id() == persona_id:
            if remaining:
                self._store.set(_DEFAULT_KEY, remaining[0]["id"])
            else:
                self._store.delete(_DEFAULT_KEY)
        return True

    def set_default(self, persona_id: str) -> bool:
        persona_id = str(persona_id or "").strip()
        if not persona_id:
            self._store.delete(_DEFAULT_KEY)
            return True
        if self.get(persona_id) is None:
            return False
        self._store.set(_DEFAULT_KEY, persona_id)
        return True

    def import_personas(self, items: Any, mode: str = "merge") -> Dict[str, int]:
        """导入人设列表。merge 跳过同 id/同名条目，overwrite 覆盖同 id/同名条目。"""
        if not isinstance(items, list):
            raise ValueError("导入内容必须是数组")
        mode = "overwrite" if str(mode or "").strip().lower() == "overwrite" else "merge"

        personas = self.list()
        by_id = {p["id"]: p for p in personas}
        added = updated = skipped = 0

        for item in items:
            persona = normalize_persona(item)
            if persona is None:
                skipped += 1
                continue
            existing = by_id.get(persona["id"])
            if existing is None:
                # 导入内容通常不带本库 id，按名称认定是同一个人设
                existing = next((p for p in personas if p["name"] == persona["name"]), None)
            if existing is not None:
                if mode == "merge":
                    skipped += 1
                    continue
                persona["id"] = existing["id"]
                persona["created_at"] = existing.get("created_at") or persona["created_at"]
                personas[personas.index(existing)] = persona
                by_id[persona["id"]] = persona
                updated += 1
                continue
            if len(personas) >= MAX_PERSONAS:
                skipped += 1
                continue
            personas.append(persona)
            by_id[persona["id"]] = persona
            added += 1

        self._store.set(_PERSONAS_KEY, personas)
        if personas and not self.default_id():
            self._store.set(_DEFAULT_KEY, personas[0]["id"])
        return {"added": added, "updated": updated, "skipped": skipped}


def get_persona_store(base_dir: Optional[str] = None) -> PersonaStore:
    """返回进程内共享的人设存储（首次调用时创建）。"""
    global _STORE
    if _STORE is None or base_dir is not None:
        _STORE = PersonaStore(base_dir)
    return _STORE


def session_persona(session: Any, store: Optional[PersonaStore] = None) -> Optional[Dict[str, Any]]:
    """解析会话绑定的人设：``persona_id`` 优先，否则回退全局默认。"""
    if not isinstance(session, dict):
        return None
    persona_id = str(session.get("persona_id") or "").strip()
    try:
        persona_store = store or get_persona_store()
        return persona_store.resolve(persona_id)
    except Exception as exc:  # pragma: no cover - 存储异常不该阻断对话
        _log.debug("[Persona] 解析人设失败: %s", exc)
        return None
