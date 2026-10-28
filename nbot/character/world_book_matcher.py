"""
世界书关键词匹配器

扫描用户消息，根据关键词匹配返回应该注入的世界书条目。
支持多源上下文召回：用户消息 + assistant 回复 + 历史上下文 + 场景状态。
"""

import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from nbot.character.models import WorldBook, WorldBookEntry

_log = logging.getLogger(__name__)

# 正则 / 整词模式编译缓存：{(pattern, case_sensitive): compiled or None}
_REGEX_CACHE: Dict[tuple, Optional["re.Pattern[str]"]] = {}

# 可作为「整词」处理的关键词：以 ASCII 字母数字开头，避免对中文强行加词边界
_WORD_LIKE_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_'\- ]*$")

# 项目根目录（本文件位于 nbot/character/）
_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# ---------- 多源召回常量 ----------

SOURCE_WEIGHTS: Dict[str, int] = {
    "user": 50,
    "scene_state": 45,
    "assistant_recent": 30,
    "history": 20,
    "always": 100,
}

ENTRY_TYPE_PRIORITY: Dict[str, int] = {
    "relationship": 90,
    "rule": 80,
    "location": 70,
    "event": 60,
    "npc": 50,
    "faction": 45,
    "lore": 40,
    "style": 35,
    "secret": 30,
}


# ---------- 多源召回数据结构 ----------

@dataclass
class WorldBookRecallContext:
    """世界书召回上下文，描述本轮匹配可使用的所有信息"""

    latest_user_message: str = ""
    recent_messages: List[Dict[str, Any]] = field(default_factory=list)
    assistant_recent_text: str = ""
    history_text: str = ""
    scene: Dict[str, Any] = field(default_factory=dict)
    active_entry_ids: List[str] = field(default_factory=list)
    character_id: str = ""
    target_id: str = ""
    scope_id: str = ""


@dataclass
class WorldBookRecallConfig:
    """世界书召回配置"""

    recent_message_limit: int = 6
    max_history_chars: int = 2000
    max_total_chars: int = 3000
    max_entries: int = 8
    max_always_chars: int = 800
    max_scene_chars: int = 1000
    max_keyword_chars: int = 1200
    max_assistant_triggered_entries: int = 3
    min_assistant_priority: int = 20
    enable_assistant_trigger: bool = True
    enable_history_trigger: bool = True
    enable_scene_trigger: bool = True
    enable_cooldown: bool = True


@dataclass
class WorldBookMatchResult:
    """世界书匹配结果"""

    entry: WorldBookEntry
    trigger_sources: List[str] = field(default_factory=list)
    matched_keywords: List[str] = field(default_factory=list)
    score: int = 0


# ---------- UUID ↔ 角色名称 解析 ----------
# 前端 character_ids 可能存的是 custom_personality_presets 的 UUID，
# 但运行时 character_id 是角色名称。需要双向解析。

_preset_uuid_to_name_cache: Dict[str, str] = {}
_preset_cache_mtime: float = 0.0
_profile_names_cache: set = set()
_profile_names_mtime: float = 0.0


def _resolve_preset_uuids(base_dir: str) -> Dict[str, str]:
    """加载 custom_personality_presets.json，返回 {uuid: name} 映射（带缓存）"""
    global _preset_uuid_to_name_cache, _preset_cache_mtime
    presets_path = os.path.join(base_dir, "data", "web", "custom_personality_presets.json")
    try:
        mtime = os.path.getmtime(presets_path)
        if mtime == _preset_cache_mtime and _preset_uuid_to_name_cache:
            return _preset_uuid_to_name_cache
        with open(presets_path, "r", encoding="utf-8") as f:
            presets = json.load(f)
        mapping = {}
        for p in presets:
            pid = p.get("id", "")
            pname = p.get("name", "")
            if pid and pname:
                mapping[pid] = pname
        _preset_uuid_to_name_cache = mapping
        _preset_cache_mtime = mtime
        return mapping
    except Exception:
        return _preset_uuid_to_name_cache


def _load_profile_names(base_dir: str) -> set:
    """加载 profiles.json 中所有角色名称（带缓存）"""
    global _profile_names_cache, _profile_names_mtime
    profiles_path = os.path.join(base_dir, "data", "character", "profiles.json")
    try:
        mtime = os.path.getmtime(profiles_path)
        if mtime == _profile_names_mtime and _profile_names_cache:
            return _profile_names_cache
        with open(profiles_path, "r", encoding="utf-8") as f:
            profiles = json.load(f)
        _profile_names_cache = set(profiles.keys())
        _profile_names_mtime = mtime
        return _profile_names_cache
    except Exception:
        return _profile_names_cache


def _character_matches(character_id: str, book_character_ids: List[str], base_dir: str = "") -> bool:
    """检查 character_id 是否匹配世界书的 character_ids（支持 UUID 和名称混合）"""
    if not book_character_ids:
        return True  # 全局生效

    # 直接匹配（名称 ↔ 名称）
    if character_id in book_character_ids:
        return True

    if not base_dir:
        return False

    # UUID → 名称 解析
    uuid_to_name = _resolve_preset_uuids(base_dir)
    profile_names = _load_profile_names(base_dir)

    # case 1: character_id 是 UUID，需要转成名称再比对
    if character_id in uuid_to_name:
        resolved_name = uuid_to_name[character_id]
        if resolved_name in book_character_ids:
            return True

    # case 2: character_ids 里有 UUID，需要转成名称再和 character_id 比对
    for cid in book_character_ids:
        if cid in uuid_to_name:
            if uuid_to_name[cid] == character_id:
                return True
        # 也检查 profiles.json 中是否有此名称
        if cid in profile_names and cid == character_id:
            return True

    return False


# ---------- 辅助函数 ----------

def _extract_assistant_recent_text(recent_messages: List[Dict[str, Any]], limit: int = 6) -> str:
    """从最近消息中提取 assistant 回复文本"""
    assistant_texts = []
    count = 0
    for msg in reversed(recent_messages):
        if msg.get("role") == "assistant" and msg.get("content"):
            assistant_texts.insert(0, msg["content"])
            count += 1
            if count >= limit:
                break
    return "\n".join(assistant_texts)


def _extract_history_text(recent_messages: List[Dict[str, Any]], max_chars: int = 2000) -> str:
    """从最近消息中提取上下文文本（user + assistant）"""
    parts = []
    total = 0
    for msg in reversed(recent_messages):
        content = msg.get("content", "")
        if not content:
            continue
        if total + len(content) > max_chars:
            break
        parts.insert(0, content)
        total += len(content)
    return "\n".join(parts)


def _extract_scene_text(scene: Dict[str, Any]) -> str:
    """从场景状态中提取可匹配文本"""
    if not scene:
        return ""
    parts = []
    for key, value in scene.items():
        if isinstance(value, str) and value:
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(v) for v in value if v)
    return " ".join(parts)


def _compile_keyword_pattern(pattern: str, case_sensitive: bool) -> Optional["re.Pattern[str]"]:
    """编译关键词正则（带缓存），非法正则返回 None 而不是抛错。"""
    key = (pattern, bool(case_sensitive))
    if key in _REGEX_CACHE:
        return _REGEX_CACHE[key]
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        compiled: Optional["re.Pattern[str]"] = re.compile(pattern, flags)
    except re.error as exc:
        _log.debug("[WorldBook] invalid regex keyword %r: %s", pattern, exc)
        compiled = None
    _REGEX_CACHE[key] = compiled
    return compiled


def _keyword_hit(text: str, keyword: str, entry: WorldBookEntry) -> bool:
    """单关键词命中判定，支持子串（默认）/ 正则 / 整词三种模式。"""
    if not text or not keyword:
        return False

    if getattr(entry, "use_regex", False):
        compiled = _compile_keyword_pattern(str(keyword), bool(entry.case_sensitive))
        return bool(compiled and compiled.search(text))

    if getattr(entry, "match_whole_words", False) and _WORD_LIKE_RE.match(str(keyword)):
        flags = 0 if entry.case_sensitive else re.IGNORECASE
        boundary = (
            r"(?<![0-9A-Za-z_])" + re.escape(str(keyword)) + r"(?![0-9A-Za-z_])"
        )
        return bool(re.search(boundary, text, flags))

    check_text = text if entry.case_sensitive else text.lower()
    check_kw = keyword if entry.case_sensitive else keyword.lower()
    return check_kw in check_text


def _check_match_keywords(text: str, entry: WorldBookEntry) -> List[str]:
    """检查文本中命中的关键词，返回命中的关键词列表"""
    matched = []
    for keyword in entry.keywords:
        if _keyword_hit(text, keyword, entry):
            matched.append(keyword)
    return matched


def _entry_probability(entry: WorldBookEntry) -> int:
    """条目触发概率（0-100），缺字段时视为 100。"""
    try:
        value = int(getattr(entry, "probability", 100))
    except (TypeError, ValueError):
        return 100
    return max(0, min(100, value))


def _probability_passes(entry: WorldBookEntry, rng: Optional[random.Random] = None) -> bool:
    """按 probability 掷骰判定本回合是否允许触发该条目。"""
    probability = _entry_probability(entry)
    if probability >= 100:
        return True
    if probability <= 0:
        return False
    roll = rng.random() if rng is not None else random.random()
    return roll * 100 < probability


def _resolve_scene_field(scene: Dict[str, Any], field_name: str) -> Any:
    """解析场景字段，支持点号路径（如 relationship.affection）。"""
    if not isinstance(scene, dict):
        return None
    if field_name in scene:
        return scene[field_name]
    current: Any = scene
    for part in str(field_name).split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _match_trigger_value(actual: Any, expected: Any) -> bool:
    """判断单个状态值与触发表达式是否匹配。

    支持：精确值、'*' 通配、字符串取反 '!value'、
    数值比较（>= <= == != > <）与区间（a..b）。
    """
    if expected == "*":
        return actual not in (None, "", [], {})

    if isinstance(actual, str):
        text = str(expected)
        if text.startswith("!") and len(text) > 1:
            return actual != text[1:]
        return actual == text

    if isinstance(actual, bool):
        return bool(expected) == actual if isinstance(expected, bool) else str(actual).lower() == str(expected).lower()

    if isinstance(actual, (int, float)):
        if isinstance(expected, (int, float)) and not isinstance(expected, bool):
            return float(actual) == float(expected)
        text = str(expected).strip()
        if ".." in text:
            try:
                low, high = text.split("..", 1)
                return float(low) <= float(actual) <= float(high)
            except ValueError:
                return False
        for op in (">=", "<=", "==", "!=", ">", "<"):
            if text.startswith(op):
                try:
                    value = float(text[len(op):].strip())
                except ValueError:
                    return False
                if op == ">=":
                    return actual >= value
                if op == "<=":
                    return actual <= value
                if op == "==":
                    return actual == value
                if op == "!=":
                    return actual != value
                if op == ">":
                    return actual > value
                return actual < value
        try:
            return float(actual) == float(text)
        except ValueError:
            return False

    return False


def _check_state_triggers(scene: Dict[str, Any], entry: WorldBookEntry) -> bool:
    """检查场景状态是否触发条目的 state_triggers"""
    if not entry.state_triggers or not scene:
        return False
    for field_name, trigger_values in entry.state_triggers.items():
        if not isinstance(trigger_values, list):
            trigger_values = [trigger_values]
        if not trigger_values:
            continue
        scene_value = _resolve_scene_field(scene, field_name)
        if scene_value is None:
            continue
        if isinstance(scene_value, list):
            if any(
                _match_trigger_value(item, expected)
                for item in scene_value
                for expected in trigger_values
            ):
                return True
        elif any(_match_trigger_value(scene_value, expected) for expected in trigger_values):
            return True
    return False


def _satisfies_match_mode(matched_keywords: List[str], entry: WorldBookEntry) -> bool:
    """检查匹配的关键词是否满足条目的 match_mode"""
    if not matched_keywords:
        return False
    if entry.match_mode == "all":
        non_empty = [k for k in entry.keywords if k]
        return len(matched_keywords) == len(non_empty)
    return True  # "any" mode


def _find_book_name(world_books: List[WorldBook], entry: WorldBookEntry) -> str:
    """查找条目所属的世界书名称"""
    for book in world_books:
        if entry in book.entries:
            return book.name
    return ""


# ---------- V2 多源召回匹配 ----------

def match_entries_v2(
    context: WorldBookRecallContext,
    world_books: List[WorldBook],
    character_id: Optional[str] = None,
    config: Optional[WorldBookRecallConfig] = None,
    rng: Optional[random.Random] = None,
) -> List[WorldBookMatchResult]:
    """多源上下文召回匹配

    Args:
        context: 召回上下文
        world_books: 已加载的世界书列表
        character_id: 当前角色 ID
        config: 召回配置
        rng: 触发概率掷骰用的随机源（测试可注入固定种子）

    Returns:
        按 score 降序排列的匹配结果列表
    """
    if not world_books:
        return []

    if config is None:
        config = WorldBookRecallConfig()

    # 构建各源文本
    user_text = context.latest_user_message
    assistant_text = context.assistant_recent_text or _extract_assistant_recent_text(
        context.recent_messages, limit=config.recent_message_limit
    )
    history_text = context.history_text or _extract_history_text(
        context.recent_messages, max_chars=config.max_history_chars
    )

    results: List[WorldBookMatchResult] = []
    assistant_triggered_count = 0

    for book in world_books:
        if not book.enabled:
            continue

        # 角色过滤
        if character_id and book.character_ids:
            if not _character_matches(character_id, book.character_ids, _BASE_DIR):
                continue

        for entry in book.entries:
            if not entry.enabled:
                continue

            # 常驻条目
            if entry.always_on:
                results.append(WorldBookMatchResult(
                    entry=entry,
                    trigger_sources=["always"],
                    matched_keywords=[],
                    score=SOURCE_WEIGHTS["always"] + entry.priority + entry.weight,
                ))
                continue

            # 跳过无关键词且非常驻的条目
            if not entry.keywords and not entry.state_triggers:
                continue

            trigger_sources: List[str] = []
            all_matched_keywords: List[str] = []
            score = 0

            # 1. 用户消息匹配
            if "user" in entry.trigger_sources and user_text:
                user_matched = _check_match_keywords(user_text, entry)
                if _satisfies_match_mode(user_matched, entry):
                    trigger_sources.append("user")
                    all_matched_keywords.extend(user_matched)
                    score += SOURCE_WEIGHTS["user"]

            # 2. assistant 回复匹配
            if (config.enable_assistant_trigger
                    and "assistant_recent" in entry.trigger_sources
                    and assistant_text
                    and entry.priority >= config.min_assistant_priority
                    and assistant_triggered_count < config.max_assistant_triggered_entries):
                asst_matched = _check_match_keywords(assistant_text, entry)
                if _satisfies_match_mode(asst_matched, entry):
                    # assistant 不触发 secret 类型
                    if entry.entry_type != "secret":
                        trigger_sources.append("assistant_recent")
                        all_matched_keywords.extend(asst_matched)
                        score += SOURCE_WEIGHTS["assistant_recent"]
                        assistant_triggered_count += 1

            # 3. 历史上下文匹配
            if (config.enable_history_trigger
                    and "history" in entry.trigger_sources
                    and history_text):
                hist_matched = _check_match_keywords(history_text, entry)
                if _satisfies_match_mode(hist_matched, entry):
                    trigger_sources.append("history")
                    all_matched_keywords.extend(hist_matched)
                    score += SOURCE_WEIGHTS["history"]

            # 4. 场景状态匹配
            if (config.enable_scene_trigger
                    and "scene_state" in entry.trigger_sources
                    and _check_state_triggers(context.scene, entry)):
                trigger_sources.append("scene_state")
                score += SOURCE_WEIGHTS["scene_state"]

            # 只有至少命中一个源才加入结果
            if trigger_sources:
                # 命中后再按条目概率决定本回合是否真的触发
                if not _probability_passes(entry, rng):
                    continue
                score += entry.priority + entry.weight
                # 去重关键词
                unique_keywords = list(dict.fromkeys(all_matched_keywords))
                results.append(WorldBookMatchResult(
                    entry=entry,
                    trigger_sources=trigger_sources,
                    matched_keywords=unique_keywords,
                    score=score,
                ))

    # 排序：score 降序 → priority 降序 → entry_type 优先级 → 内容长度升序
    results.sort(key=lambda r: (
        -r.score,
        -r.entry.priority,
        -ENTRY_TYPE_PRIORITY.get(r.entry.entry_type, 0),
        len(r.entry.content),
    ))

    # 裁剪：max_entries + 总字符预算（至少保留一条，单条超长由注入器截断）
    trimmed: List[WorldBookMatchResult] = []
    total_chars = 0
    for result in results:
        if len(trimmed) >= config.max_entries:
            break
        content_len = len(result.entry.content or "")
        if trimmed and total_chars + content_len > config.max_total_chars:
            continue
        trimmed.append(result)
        total_chars += content_len
    results = trimmed

    if results:
        _log.debug(
            "[WorldBook] v2 matched %d entries: %s",
            len(results),
            [(r.entry.name, r.trigger_sources, r.score) for r in results],
        )

    return results


# ---------- V2 测试匹配 ----------

def debug_match_entries(
    context: "WorldBookRecallContext",
    world_books: List[WorldBook],
    character_id: Optional[str] = None,
    config: Optional[WorldBookRecallConfig] = None,
    rng: Optional[random.Random] = None,
) -> List[dict]:
    """逐条目诊断世界书匹配结果（调试面板用）

    返回所有条目（含未命中者）的诊断信息，解释每一条为何命中/未命中。
    """
    if config is None:
        config = WorldBookRecallConfig()

    user_text = context.latest_user_message
    assistant_text = context.assistant_recent_text or _extract_assistant_recent_text(
        context.recent_messages, limit=config.recent_message_limit
    )
    history_text = context.history_text or _extract_history_text(
        context.recent_messages, max_chars=config.max_history_chars
    )

    diagnostics: List[dict] = []
    assistant_triggered_count = 0

    for book in world_books:
        book_enabled = book.enabled
        for entry in book.entries:
            item = {
                "world_book_id": book.id,
                "world_book_name": book.name,
                "entry_id": entry.id,
                "entry_name": entry.name,
                "enabled": entry.enabled,
                "always_on": entry.always_on,
                "entry_type": entry.entry_type,
                "priority": entry.priority,
                "position": getattr(entry, "position", "") or "",
                "depth": getattr(entry, "depth", 4),
                "probability": _entry_probability(entry),
                "use_regex": bool(getattr(entry, "use_regex", False)),
                "match_whole_words": bool(getattr(entry, "match_whole_words", False)),
                "matched": False,
                "injected": False,
                "matched_keywords": [],
                "missed_keywords": [],
                "trigger_sources": [],
                "score": 0,
                "reason": "",
                "content_preview": (entry.content or "")[:200],
            }

            if not book_enabled:
                item["reason"] = "世界书已禁用"
                diagnostics.append(item)
                continue
            if character_id and book.character_ids and not _character_matches(
                character_id, book.character_ids, _BASE_DIR
            ):
                item["reason"] = "世界书未绑定当前角色"
                diagnostics.append(item)
                continue
            if not entry.enabled:
                item["reason"] = "条目已禁用"
                diagnostics.append(item)
                continue

            if entry.always_on:
                item["matched"] = True
                item["injected"] = True
                item["trigger_sources"] = ["always"]
                item["score"] = SOURCE_WEIGHTS["always"] + entry.priority + entry.weight
                diagnostics.append(item)
                continue

            if not entry.keywords and not entry.state_triggers:
                item["reason"] = "无关键词且非常驻条目"
                diagnostics.append(item)
                continue

            trigger_sources: List[str] = []
            all_matched: List[str] = []
            score = 0
            notes: List[str] = []

            if "user" in entry.trigger_sources and user_text:
                user_matched = _check_match_keywords(user_text, entry)
                if _satisfies_match_mode(user_matched, entry):
                    trigger_sources.append("user")
                    all_matched.extend(user_matched)
                    score += SOURCE_WEIGHTS["user"]
                elif entry.match_mode == "all" and user_matched:
                    notes.append("全匹配只命中部分关键词")

            if (
                config.enable_assistant_trigger
                and "assistant_recent" in entry.trigger_sources
                and assistant_text
            ):
                if entry.priority < config.min_assistant_priority:
                    notes.append(
                        f"assistant 源优先级低于阈值({config.min_assistant_priority})"
                    )
                elif assistant_triggered_count >= config.max_assistant_triggered_entries:
                    notes.append("assistant 源触发条目数已达上限")
                else:
                    asst_matched = _check_match_keywords(assistant_text, entry)
                    if _satisfies_match_mode(asst_matched, entry):
                        if entry.entry_type != "secret":
                            trigger_sources.append("assistant_recent")
                            all_matched.extend(asst_matched)
                            score += SOURCE_WEIGHTS["assistant_recent"]
                            assistant_triggered_count += 1
                        else:
                            notes.append("secret 条目不允许 assistant 源触发")

            if (
                config.enable_history_trigger
                and "history" in entry.trigger_sources
                and history_text
            ):
                hist_matched = _check_match_keywords(history_text, entry)
                if _satisfies_match_mode(hist_matched, entry):
                    trigger_sources.append("history")
                    all_matched.extend(hist_matched)
                    score += SOURCE_WEIGHTS["history"]

            if (
                config.enable_scene_trigger
                and "scene_state" in entry.trigger_sources
            ):
                if not context.scene:
                    notes.append("无场景状态数据")
                elif _check_state_triggers(context.scene, entry):
                    trigger_sources.append("scene_state")
                    score += SOURCE_WEIGHTS["scene_state"]
                else:
                    notes.append("状态触发条件未满足")

            item["trigger_sources"] = trigger_sources
            item["matched_keywords"] = list(dict.fromkeys(all_matched))
            item["score"] = score + entry.priority + entry.weight if trigger_sources else 0

            # 命中后再按条目概率判定本回合是否触发
            if trigger_sources and not _probability_passes(entry, rng):
                notes.append(f"命中但未通过触发概率({_entry_probability(entry)}%)")
                item["probability_skipped"] = True
                item["trigger_sources"] = []
                item["matched_keywords"] = []
                item["score"] = 0
                trigger_sources = []

            check_text = user_text or ""
            item["missed_keywords"] = [
                kw
                for kw in entry.keywords
                if kw and not _keyword_hit(check_text, kw, entry)
            ]

            if trigger_sources:
                item["matched"] = True
                item["injected"] = True
            elif notes:
                item["reason"] = "；".join(dict.fromkeys(notes))
            elif not user_text:
                item["reason"] = "消息为空"
            else:
                item["reason"] = "关键词未命中"

            diagnostics.append(item)

    # 按注入预算标记被裁剪的命中条目
    injected = [d for d in diagnostics if d.get("injected")]
    injected.sort(key=lambda d: (-d["score"], -d["priority"]))
    total_chars = 0
    count = 0
    for d in injected:
        content_len = len(d.get("content_preview") or "")
        if count >= config.max_entries:
            d["injected"] = False
            d["reason"] = f"命中但超出条目数上限({config.max_entries})"
            continue
        if count > 0 and total_chars + content_len > config.max_total_chars:
            d["injected"] = False
            d["reason"] = f"命中但超出总字符预算({config.max_total_chars})"
            continue
        count += 1
        total_chars += content_len

    return diagnostics


def test_match_v2(
    context: WorldBookRecallContext,
    world_books: List[WorldBook],
    character_id: Optional[str] = None,
    config: Optional[WorldBookRecallConfig] = None,
) -> List[dict]:
    """测试多源关键词匹配，返回详细的匹配信息（用于调试）

    Returns:
        [{"world_book_name": "...", "entry_name": "...", "matched_keywords": [...],
          "trigger_sources": [...], "score": 95, "content_preview": "..."}]
    """
    results = match_entries_v2(context, world_books, character_id, config)
    return [
        {
            "world_book_name": _find_book_name(world_books, r.entry),
            "entry_name": r.entry.name,
            "entry_id": r.entry.id,
            "matched_keywords": r.matched_keywords,
            "trigger_sources": r.trigger_sources,
            "score": r.score,
            "content_preview": r.entry.content[:200],
        }
        for r in results
    ]


# ---------- V1 向后兼容接口 ----------

def match_entries(
    user_message: str,
    world_books: List[WorldBook],
    character_id: Optional[str] = None,
    max_total_chars: int = 3000,
) -> List[WorldBookEntry]:
    """扫描用户消息，返回所有命中的世界书条目（V1 兼容接口）

    内部使用 match_entries_v2 实现，保持向后兼容。

    Args:
        user_message: 用户发送的消息文本
        world_books: 已加载的世界书列表
        character_id: 当前角色 ID（用于过滤关联的世界书）
        max_total_chars: 命中条目总字符上限

    Returns:
        按 priority 降序排列的命中条目列表
    """
    if not user_message or not world_books:
        return []

    context = WorldBookRecallContext(latest_user_message=user_message)
    config = WorldBookRecallConfig(max_total_chars=max_total_chars)
    results = match_entries_v2(context, world_books, character_id, config)
    return [r.entry for r in results]


def _check_match(message: str, entry: WorldBookEntry) -> bool:
    """检查消息是否命中条目关键词（V1 兼容）"""
    matched = _check_match_keywords(message or "", entry)
    return _satisfies_match_mode(matched, entry)


def test_match(
    user_message: str,
    world_books: List[WorldBook],
    character_id: Optional[str] = None,
) -> List[dict]:
    """测试关键词匹配，返回详细的匹配信息（V1 兼容接口）

    Returns:
        [{"world_book_name": "...", "entry_name": "...", "matched_keywords": [...]}]
    """
    if not user_message or not world_books:
        return []

    context = WorldBookRecallContext(latest_user_message=user_message)
    v2_results = test_match_v2(context, world_books, character_id)

    # 转换为 V1 格式
    return [
        {
            "world_book_name": r["world_book_name"],
            "world_book_id": r.get("entry_id", ""),
            "entry_name": r["entry_name"],
            "entry_id": r.get("entry_id", ""),
            "matched_keywords": r["matched_keywords"],
            "content_preview": r["content_preview"],
        }
        for r in v2_results
    ]
