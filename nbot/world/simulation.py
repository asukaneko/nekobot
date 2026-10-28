"""世界模拟引擎（WorldSimulation）

与 ``nbot.world.engine`` 的群聊判定器不同，这里负责「世界内推进」：
玩家一次行动 → 模型产出结构化结果（展示片段 / 状态增量 / 新事件 / 建议行动）
→ 落盘为事件并推进节点状态。

模型不可用、返回非 JSON、或返回内容空转时，自动降级为本地规则推进
（``ai_used=False``），保证世界永远能往前走一步。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nbot.world.store import NODE_EVENT_TYPES, WorldStore

_log = logging.getLogger(__name__)

DISPLAY_TYPES = ("narration", "dialogue", "action", "thought", "system_note")
_MAX_SUGGESTED_ACTIONS = 6
_MAX_DISPLAY_ENTRIES = 12
_MAX_NEW_EVENTS = 8

_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)

_SYSTEM_PROMPT = """你是「世界模拟引擎」的叙事裁定者，负责推进一个持续演化的世界。

要求：
1. 只输出一个 JSON 对象，不要输出解释、不要用 Markdown 代码块。
2. JSON 结构：
{
  "display": [{"type": "narration|dialogue|action|thought", "content": "...", "actor_id": "", "actor_name": ""}],
  "state_delta": {"global_state": {"tension": "+5"}, "characters": {"char_id": {"memory_append": ["..."]}}},
  "new_events": [{"type": "world_event|state_change", "title": "...", "content": "..."}],
  "suggested_actions": ["...", "..."],
  "branch_hint": "是否适合创建分支的一句话说明"
}
3. state_delta 用增量写法："+5" / "-3" 表示相对变化；列表追加用 "<字段>_append"。
4. 不得篡改世界基础设定（天气、时代、地理、已有角色身份），可以推进它们。
5. 没有实际变化时，display 也至少给一条 narration。
"""


@dataclass
class WorldStepResult:
    """一次世界推进（step / tick / jump_time）的结果"""

    world_id: str = ""
    node_id: str = ""
    action_type: str = "act"
    ai_used: bool = False
    state: Dict[str, Any] = field(default_factory=dict)
    display: List[Dict[str, Any]] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    suggested_actions: List[str] = field(default_factory=list)
    branch_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "world_id": self.world_id,
            "node_id": self.node_id,
            "action_type": self.action_type,
            "ai_used": self.ai_used,
            "state": self.state,
            "display": self.display,
            "events": self.events,
            "suggested_actions": self.suggested_actions,
            "branch_hint": self.branch_hint,
        }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(item) for item in value if _as_text(item))
    return str(value).strip()


def parse_json_payload(raw: Any) -> Optional[Dict[str, Any]]:
    """从模型输出里抽出 JSON 对象，失败返回 None。"""
    if isinstance(raw, dict):
        return raw
    text = _as_text(raw)
    if not text:
        return None

    candidates = [text]
    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates.extend(block.strip() for block in fenced)
    block = _JSON_BLOCK_RE.search(text)
    if block:
        candidates.append(block.group(0))

    for candidate in candidates:
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _clean_display(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    cleaned: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = _as_text(item.get("content"))
        if not content:
            continue
        kind = _as_text(item.get("type")).lower()
        if kind not in DISPLAY_TYPES:
            kind = "narration"
        entry = {"type": kind, "content": content}
        actor_id = _as_text(item.get("actor_id"))
        actor_name = _as_text(item.get("actor_name"))
        if actor_id:
            entry["actor_id"] = actor_id
        if actor_name:
            entry["actor_name"] = actor_name
        cleaned.append(entry)
        if len(cleaned) >= _MAX_DISPLAY_ENTRIES:
            break
    return cleaned


def _clean_new_events(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    cleaned: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        content = _as_text(item.get("content"))
        if not content:
            continue
        kind = _as_text(item.get("type")).lower()
        if kind not in NODE_EVENT_TYPES or kind in ("branch", "player_action"):
            kind = "world_event"
        cleaned.append(
            {
                "type": kind,
                "title": _as_text(item.get("title")),
                "content": content,
            }
        )
        if len(cleaned) >= _MAX_NEW_EVENTS:
            break
    return cleaned


def _clean_suggested_actions(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []

    actions: List[str] = []
    for item in raw:
        text = _as_text(item)
        if not text or text in actions:
            continue
        actions.append(text)
        if len(actions) >= _MAX_SUGGESTED_ACTIONS:
            break
    return actions


def _clean_state_delta(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): value for key, value in raw.items()}


class WorldSimulation:
    """世界模拟引擎（由 WorldStore 持久化驱动）"""

    def __init__(self, store: WorldStore, model_client: Any = None):
        self.store = store
        self.model_client = model_client

    # ------------------------------------------------------------------ #
    # 推进
    # ------------------------------------------------------------------ #

    def step(
        self,
        world_id: str,
        node_id: str,
        content: str,
        target_character_id: str = "",
        action_type: str = "act",
    ) -> WorldStepResult:
        """玩家一次行动 → 世界推进一格。"""
        action = _as_text(content)
        if not action:
            raise ValueError("content is required")

        bundle = self.store.get_world_bundle(world_id)
        node = self._require_node(world_id, node_id)
        state_before = node.get("state") or {}

        payload: Optional[Dict[str, Any]] = None
        if self.model_client is not None:
            messages = self._build_messages(bundle, state_before, action, target_character_id)
            try:
                payload = parse_json_payload(self.model_client(messages))
            except Exception as exc:  # 模型层异常一律降级
                _log.warning("[WorldSimulation] 模型调用失败: %s", exc)
                payload = None

        cleaned = self._clean_payload(payload)
        if cleaned is None:
            return self._fallback_step(world_id, node_id, action, action_type)

        display = cleaned["display"]
        state_delta = cleaned["state_delta"]
        new_events = cleaned["new_events"]

        events: List[Dict[str, Any]] = []
        events.append(
            self.store.add_event(
                world_id,
                node_id,
                "player_action",
                action,
                title="玩家行动",
                state_delta=state_delta or None,
                extra={"action_type": str(action_type or "act")},
            )
        )
        for entry in display:
            events.append(
                self.store.add_event(
                    world_id,
                    node_id,
                    entry["type"],
                    entry["content"],
                    actor_id=entry.get("actor_id", ""),
                    actor_name=entry.get("actor_name", ""),
                )
            )
        for item in new_events:
            events.append(
                self.store.add_event(
                    world_id, node_id, item["type"], item["content"], title=item["title"]
                )
            )

        result = WorldStepResult(
            world_id=world_id,
            node_id=node_id,
            action_type=str(action_type or "act"),
            ai_used=True,
            state=self._current_state(world_id, node_id),
            display=display,
            events=events,
            suggested_actions=cleaned["suggested_actions"],
            branch_hint=cleaned["branch_hint"],
        )
        return result

    def _fallback_step(
        self, world_id: str, node_id: str, action: str, action_type: str
    ) -> WorldStepResult:
        note = f"（本地规则推进）你{action}，周围暂时没有更多变化。"
        display = [{"type": "system_note", "content": note}]
        events = [
            self.store.add_event(
                world_id,
                node_id,
                "player_action",
                action,
                title="玩家行动",
                extra={"action_type": str(action_type or "act")},
            ),
            self.store.add_event(world_id, node_id, "system_note", note, title="规则推进"),
        ]
        return WorldStepResult(
            world_id=world_id,
            node_id=node_id,
            action_type=str(action_type or "act"),
            ai_used=False,
            state=self._current_state(world_id, node_id),
            display=display,
            events=events,
            suggested_actions=[],
            branch_hint="",
        )

    def _clean_payload(self, payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """校验模型输出；完全无效果时返回 None（触发降级）。"""
        if not isinstance(payload, dict):
            return None
        display = _clean_display(payload.get("display"))
        new_events = _clean_new_events(payload.get("new_events"))
        state_delta = _clean_state_delta(payload.get("state_delta"))
        if not display and not new_events and not state_delta:
            return None
        return {
            "display": display,
            "new_events": new_events,
            "state_delta": state_delta,
            "suggested_actions": _clean_suggested_actions(payload.get("suggested_actions")),
            "branch_hint": _as_text(payload.get("branch_hint")),
        }

    def tick(
        self, world_id: str, node_id: str, elapsed_seconds: int = 60
    ) -> WorldStepResult:
        """时间推进（不调用模型），按世界时间倍率换算。"""
        node = self._require_node(world_id, node_id)
        state = node.get("state") if isinstance(node.get("state"), dict) else {}
        world_time = dict(state.get("world_time") or {})

        world = self.store.get_world(world_id) or {}
        time_mode = str(world_time.get("time_mode") or world.get("time_mode") or "real")
        time_scale = max(1, int(world_time.get("time_scale") or world.get("time_scale") or 1))
        try:
            elapsed = max(0, int(elapsed_seconds))
        except (TypeError, ValueError):
            elapsed = 60

        scaled = 0 if time_mode == "paused" else elapsed * time_scale
        total = int(world_time.get("elapsed_seconds_total") or 0) + scaled
        world_time["elapsed_seconds_total"] = total
        world_time["display"] = world_time.get("display") or world.get("current_time", "")
        world_time["time_mode"] = time_mode
        world_time["time_scale"] = time_scale

        delta = {"world_time": world_time}
        note = f"时间推进 {elapsed} 秒（世界内 {scaled} 秒）。"
        event = self.store.add_event(
            world_id, node_id, "system_note", note, title="时间推进", state_delta=delta
        )
        return WorldStepResult(
            world_id=world_id,
            node_id=node_id,
            action_type="tick",
            ai_used=False,
            state=self._current_state(world_id, node_id),
            display=[{"type": "system_note", "content": note}],
            events=[event],
            suggested_actions=[],
        )

    def jump_time(self, world_id: str, node_id: str, target_time: str) -> WorldStepResult:
        """时间跳转（不调用模型）。"""
        target = _as_text(target_time)
        if not target:
            raise ValueError("target_time is required")

        node = self._require_node(world_id, node_id)
        state = node.get("state") if isinstance(node.get("state"), dict) else {}
        world_time = dict(state.get("world_time") or {})
        world_time["display"] = target

        note = f"时间跳转到「{target}」。"
        event = self.store.add_event(
            world_id,
            node_id,
            "system_note",
            note,
            title="时间跳转",
            state_delta={"world_time": world_time},
        )
        return WorldStepResult(
            world_id=world_id,
            node_id=node_id,
            action_type="jump_time",
            ai_used=False,
            state=self._current_state(world_id, node_id),
            display=[{"type": "system_note", "content": note}],
            events=[event],
            suggested_actions=[],
        )

    # ------------------------------------------------------------------ #
    # 摘要 / 世界生成
    # ------------------------------------------------------------------ #

    def generate_node_summary(self, world_id: str, node_id: str) -> Dict[str, Any]:
        """生成节点摘要：有模型则润色，否则用规则摘要。"""
        node = self.store.summarize_node(world_id, node_id)
        summary = node.get("summary", "")
        if self.model_client is None:
            return node

        events = self.store.list_events(world_id, node_id)
        transcript = "\n".join(
            f"- [{item.get('type')}] {item.get('content', '')}" for item in events[-30:]
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是剧情摘要器。用 3-5 句话概括这段世界剧情的关键变化，"
                    "只输出摘要正文，不要标题、不要 JSON。"
                ),
            },
            {
                "role": "user",
                "content": f"节点：{node.get('title') or node.get('branch_name')}\n事件：\n{transcript}",
            },
        ]
        try:
            polished = _as_text(self.model_client(messages))
        except Exception as exc:
            _log.warning("[WorldSimulation] 摘要模型调用失败: %s", exc)
            polished = ""

        if polished:
            summary = polished
            node = self.store.update_node(
                world_id, node_id, {"summary": summary, "summary_source": "ai"}
            )
        return node

    def generate_world(self, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """AI 生成一个新世界（含角色与资产），失败时退回仅用输入数据建世界。"""
        payload = dict(data or {})
        world_data = self._build_world_payload(payload)

        bundle = self.store.create_world(world_data)
        world_id = bundle["world"]["world_id"]

        for character in world_data.get("characters") or []:
            if isinstance(character, dict) and str(character.get("name") or "").strip():
                try:
                    self.store.upsert_character(world_id, character)
                except ValueError:
                    continue
        for asset in world_data.get("assets") or []:
            if isinstance(asset, dict) and str(asset.get("name") or "").strip():
                try:
                    self.store.upsert_asset(world_id, asset)
                except ValueError:
                    continue
        return self.store.get_world_bundle(world_id)

    def _build_world_payload(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        name = _as_text(payload.get("name") or payload.get("summary") or "")
        fallback = {
            "name": name or "未命名世界",
            "genre": _as_text(payload.get("genre")),
            "tone": _as_text(payload.get("tone")),
            "description": _as_text(payload.get("summary")),
            "main_plot": _as_text(payload.get("summary")),
            "current_time": _as_text(payload.get("current_time")),
            "weather": _as_text(payload.get("weather")),
        }
        if self.model_client is None:
            return fallback

        messages = [
            {
                "role": "system",
                "content": (
                    "你是世界设定生成器。只输出一个 JSON 对象：\n"
                    '{"name": "", "genre": "", "tone": "", "description": "", '
                    '"current_time": "", "weather": "", "tension": 0, "main_plot": "", '
                    '"characters": [{"name": "", "role": "", "personality": "", "goals": []}], '
                    '"assets": [{"name": "", "asset_type": "location", "description": ""}]}\n'
                    "不要输出解释或 Markdown。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            generated = parse_json_payload(self.model_client(messages))
        except Exception as exc:
            _log.warning("[WorldSimulation] 世界生成模型调用失败: %s", exc)
            generated = None

        if not generated:
            return fallback

        merged = dict(fallback)
        for key in (
            "name",
            "genre",
            "tone",
            "description",
            "current_time",
            "weather",
            "tension",
            "main_plot",
        ):
            value = generated.get(key)
            if value not in (None, ""):
                merged[key] = value
        if not _as_text(merged.get("name")):
            merged["name"] = "未命名世界"
        if isinstance(generated.get("characters"), list):
            merged["characters"] = generated["characters"]
        if isinstance(generated.get("assets"), list):
            merged["assets"] = generated["assets"]
        return merged

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _require_node(self, world_id: str, node_id: str) -> Dict[str, Any]:
        node = self.store.get_node(world_id, node_id)
        if not node:
            raise KeyError(node_id)
        return node

    def _current_state(self, world_id: str, node_id: str) -> Dict[str, Any]:
        node = self.store.get_node(world_id, node_id) or {}
        state = node.get("state")
        return state if isinstance(state, dict) else {}

    def _build_messages(
        self,
        bundle: Dict[str, Any],
        state: Dict[str, Any],
        action: str,
        target_character_id: str,
    ) -> List[Dict[str, str]]:
        world = bundle.get("world") or {}
        node = bundle.get("current_node") or {}
        recent_events = [
            f"- [{item.get('type')}] {item.get('content', '')}"
            for item in (bundle.get("events") or [])[-8:]
        ]
        system_content = "\n\n".join(
            part
            for part in (
                _SYSTEM_PROMPT,
                "当前世界："
                + json.dumps(
                    {
                        "name": world.get("name", ""),
                        "genre": world.get("genre", ""),
                        "tone": world.get("tone", ""),
                        "description": world.get("description", ""),
                        "main_plot": world.get("main_plot", ""),
                    },
                    ensure_ascii=False,
                ),
                "节点：" + json.dumps(
                    {
                        "title": node.get("title", ""),
                        "branch": node.get("branch_name", ""),
                    },
                    ensure_ascii=False,
                ),
            )
            if part
        )
        user_content = "\n".join(
            [
                f"user_action: {action}",
                f"target_character_id: {target_character_id or ''}",
                "state: " + json.dumps(state, ensure_ascii=False),
                "recent_events:",
                "\n".join(recent_events) or "（无）",
            ]
        )
        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ]
