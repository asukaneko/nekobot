"""
MemoryFS — 记忆逻辑文件系统

作为现有 PromptManager 记忆系统的上层组织层。
通过 path 字段把记忆条目组织成角色可读的逻辑视图。

存储：data/web/memory_fs.json（path → MemoryFile 的索引）
注意：MemoryFS 独立持久化到 memory_fs.json，不自动写入底层 memory_service。
如需同步到底层记忆系统，需在调用方显式处理。
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from typing import Any

from nbot.memory.models import MemoryFile

_log = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = "data/web"
_FS_INDEX_FILE = "memory_fs.json"

# 防止单文件无限膨胀
_MAX_MEMORY_FILE_CHARS = 4000
_MAX_DIARY_ENTRIES = 30
_MAX_PLOT_ENTRIES = 50
_MAX_LIFE_SIM_ENTRIES = 10   # life_sim 单文件最多持久化的条目数（按会话隔离）
_MAX_LIFE_SIM_INJECT = 5     # 提示词注入时倒序取最新 N 条
_MAX_TIMELINE_ENTRIES = 15  # 跨会话时间线注入 prompt 的最近条目数
_MAX_TIMELINE_STORE = 80    # 时间线文件最多持久化的条目数

# 跨会话 timeline 提示词注入策略
_TIMELINE_PER_CONVERSATION = 2  # 每个其他会话最多取最新 N 条
_TIMELINE_MAX_TOTAL = 10        # 全局最多注入 N 条
_TIMELINE_ENTRY_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\](?:\s*\[(?P<conv>[^\]]+)\])?\s*(?P<body>.*)$"
)

_MEMORY_CATEGORY_META = {
    "user_persona": {"label": "用户人格", "injects_to_prompt": True, "order": 10},
    "character_persona": {"label": "角色人格", "injects_to_prompt": True, "order": 20},
    "important_event": {"label": "重要事件", "injects_to_prompt": True, "order": 30},
    "timeline": {"label": "跨会话时间线", "injects_to_prompt": True, "order": 35},
    "life_sim": {"label": "角色生活片段", "injects_to_prompt": True, "order": 38},
    "recent_digest": {"label": "近期摘要", "injects_to_prompt": True, "order": 40},
    "legacy": {"label": "旧版/其他", "injects_to_prompt": False, "order": 90},
}

_MEMORY_CATEGORY_ALIASES = {
    "user": "user_persona",
    "user_profile": "user_persona",
    "user_preference": "user_persona",
    "user_preferences": "user_persona",
    "persona_user": "user_persona",
    "character": "character_persona",
    "character_profile": "character_persona",
    "character_attitude": "character_persona",
    "persona_character": "character_persona",
    "relationship": "character_persona",
    "event": "important_event",
    "events": "important_event",
    "important_events": "important_event",
    "plot": "important_event",
    "plot_summary": "important_event",
    "world_event": "important_event",
    "digest": "recent_digest",
    "summary": "recent_digest",
    "recent_summary": "recent_digest",
    "dialogue_digest": "recent_digest",
    "diary": "recent_digest",
    "timeline_event": "timeline",
    "timeline_event_other": "timeline",
    "life_event": "life_sim",
    "life_sim_event": "life_sim",
    "heartbeat_life": "life_sim",
}

# 全局单例
_memory_fs: MemoryFS | None = None


def _truncate_entries(content: str, path: str) -> str:
    """截断过长内容，保留最近的条目，不切断单条记忆。

    根据路径类型选择不同的保留策略：
    - diary: 保留最近 _MAX_DIARY_ENTRIES 条
    - plot:  保留最近 _MAX_PLOT_ENTRIES 条
    - timeline: 保留最近 _MAX_TIMELINE_STORE 条
    - 其他:  按条目从旧到新丢弃，直到总长度不超限
    """
    entries = content.split("\n\n")
    if "diary" in path:
        max_entries = _MAX_DIARY_ENTRIES
    elif "plot" in path:
        max_entries = _MAX_PLOT_ENTRIES
    elif "life_sim" in path:
        max_entries = _MAX_LIFE_SIM_ENTRIES
    elif "timeline" in path:
        max_entries = _MAX_TIMELINE_STORE
    else:
        max_entries = len(entries)  # 不限条目数，只限总字符

    if len(entries) > max_entries:
        entries = entries[-max_entries:]

    # 按条目从旧到新丢弃，直到总长度不超 _MAX_MEMORY_FILE_CHARS
    while entries and len("\n\n".join(entries)) > _MAX_MEMORY_FILE_CHARS:
        entries.pop(0)

    return "\n\n".join(entries)


def _tail_entries(content: str, max_entries: int) -> str:
    """返回 content 中最近的 max_entries 条记录，按条目分隔。

    用于 prompt 注入时取最近 N 条，避免把全部历史塞入上下文。
    """
    if not content:
        return ""
    entries = [e for e in content.split("\n\n") if e.strip()]
    if len(entries) > max_entries:
        entries = entries[-max_entries:]
    return "\n\n".join(entries)


def _parse_timeline_entries(content: str) -> list[dict]:
    """解析 timeline.md 内容为结构化条目。

    支持两种格式：
    - 新格式：`[YYYY-MM-DD HH:MM] [conv:web_abc123] 标题: 内容`
    - 旧格式：`[YYYY-MM-DD HH:MM] 标题: 内容`（无 conv 标识，归入 __legacy__ 桶）

    `conv:` 前缀在存储层用于人眼可读；解析层会去掉前缀，
    让 `e["conv"]` 直接是会话标识本身，便于代码层比较。

    无法解析的整段会作为 raw 条目保留，避免历史数据丢失。
    """
    if not content:
        return []
    out: list[dict] = []
    for raw_entry in content.split("\n\n"):
        text = raw_entry.strip()
        if not text:
            continue
        m = _TIMELINE_ENTRY_RE.match(text)
        if m:
            raw_conv = (m.group("conv") or "").strip()
            # 去掉 conv: 前缀，保留可读性同时让 API 层拿到纯标识
            conv = raw_conv[5:] if raw_conv.startswith("conv:") else raw_conv
            out.append(
                {
                    "ts": (m.group("ts") or "").strip(),
                    "conv": conv,
                    "body": (m.group("body") or "").strip(),
                    "raw": text,
                }
            )
        else:
            out.append({"ts": "", "conv": "", "body": text, "raw": text})
    return out


def format_timeline_for_prompt(
    timeline_content: str,
    current_conversation_id: str = "",
    *,
    per_conversation: int = _TIMELINE_PER_CONVERSATION,
    max_total: int = _TIMELINE_MAX_TOTAL,
) -> str:
    """按会话分桶，生成 timeline 注入文本。

    策略：
    1. 解析所有条目，按 conv_id 分桶（无 conv 标识的归入 __legacy__）
    2. 排除当前会话（避免与 events/{conv_id} 重复）
    3. 每桶按时间倒序取最新 per_conversation 条
    4. 合并所有桶按时间倒序取最新 max_total 条
    5. 按时间正序输出 + 顶部说明（告诉角色这些是"其他会话"）

    显式说明：这些条目来自**其他会话/场景**的经历摘要，
    角色在当前对话中可作为背景参考，但不要当作"刚发生"叙述，
    也不要编造新细节。
    """
    if not timeline_content:
        return ""

    entries = _parse_timeline_entries(timeline_content)
    if not entries:
        return ""

    # 排除当前会话
    if current_conversation_id:
        entries = [e for e in entries if e["conv"] != current_conversation_id]

    # 按 conv_id 分桶
    buckets: dict[str, list[dict]] = {}
    for e in entries:
        key = e["conv"] or "__legacy__"
        buckets.setdefault(key, []).append(e)

    # 每桶按时间倒序取最新 per_conversation 条
    selected: list[dict] = []
    for bucket in buckets.values():
        bucket_sorted = sorted(
            bucket,
            key=lambda x: x["ts"] or "",
            reverse=True,
        )
        selected.extend(bucket_sorted[:per_conversation])

    # 全局按时间倒序取 max_total
    selected.sort(key=lambda x: x["ts"] or "", reverse=True)
    selected = selected[:max_total]

    if not selected:
        return ""

    # 按时间正序输出（叙事顺序，便于阅读）
    selected.sort(key=lambda x: x["ts"] or "")

    lines = [
        "以下条目来自角色与该用户的**其他会话/场景**（非当前对话）的近期生活经历摘要。",
        f"格式：`[时间] [会话标识] 事件摘要`。每个其他会话最多展示最新 {per_conversation} 条，"
        f"全局最多 {max_total} 条。",
        "这些经历发生在当前对话之外；当前会话的具体事件由下方 events/plot 段承载，不在此处重复。",
        "如果用户话题明确引用了某段过去经历，角色可以自然回忆/承认；",
        "否则不要把这些事件当作'刚发生的事'叙述，也不要编造新细节。",
        "",
    ]
    for e in selected:
        ts = e["ts"] or "—"
        if e["conv"]:
            conv_label = f"[{e['conv']}]"
        else:
            conv_label = "[legacy]"
        lines.append(f"- [{ts}] {conv_label} {e['body']}")
    return "\n".join(lines)


def format_life_sim_for_prompt(
    life_sim_content: str,
    *,
    max_entries: int = _MAX_LIFE_SIM_INJECT,
) -> str:
    """格式化 life_sim 内容注入 prompt：倒序取最新 N 条 + header 说明。

    life_sim 是按会话隔离的角色生活片段文件
    （路径规范：characters/{char_id}/life_sim/{conversation_id}.md），
    由静默心跳（silent heartbeat）持续 append 生成。
    这些条目代表"用户不在场时"角色的独自生活片段（思考、行动、情绪、状态）。

    注入策略：
    1. 解析条目（按 ``\\n\\n`` 分隔，每条 ``[timestamp] content``）
    2. 倒序取最新 max_entries 条（默认 _MAX_LIFE_SIM_INJECT=5）
    3. 顶部加说明文字，强调这些是"用户不在场时"的生活片段，
       避免与当前对话事件混淆

    与 timeline 的区别：
    - timeline 是"其他会话发生过什么"（跨会话）
    - life_sim 是"本会话之外，角色在做什么"（独处时的生活）
    """
    if not life_sim_content:
        return ""

    entries = [e.strip() for e in life_sim_content.split("\n\n") if e.strip()]
    if not entries:
        return ""

    # 倒序取最新 N 条
    if len(entries) > max_entries:
        entries = entries[-max_entries:]

    # 再正序输出（时间正序便于阅读）
    lines = [
        "以下是该角色在**当前会话/对话之外**的近期生活片段（由静默心跳持续生成）。",
        "这些片段描述的是**用户不在场时**，角色独自进行的生活活动"
        "（思考、行动、情绪、状态等）。",
        f"最多展示最新 {max_entries} 条；更早的片段已自动归档丢弃。",
        "当前会话的具体对话事件由下方 events/plot 段承载，不在此处重复。",
        "如果当前对话话题与某段生活片段自然相关，角色可以回忆/承认；",
        "否则不要把这些活动当作'刚发生的事'叙述，也不要编造新细节。",
        "",
    ]
    lines.extend(f"- {e}" for e in entries)
    return "\n".join(lines)


def normalize_memory_category(value) -> str:
    category = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if category in _MEMORY_CATEGORY_META and category != "legacy":
        return category
    return _MEMORY_CATEGORY_ALIASES.get(category, "")


_TERM_RE = re.compile(r"[a-zA-Z0-9_]{2,}|[\u3400-\u9fff\uf900-\ufaff]{2,}")


def _query_terms(query: str) -> list[str]:
    """从查询中提取匹配词：拉丁词元 + CJK 二元组（滑动窗口）。"""
    terms: list[str] = []
    for token in _TERM_RE.findall(str(query or "")):
        if token[0].isascii():
            terms.append(token.lower())
        elif len(token) == 2:
            terms.append(token)
        else:
            terms.extend(token[i:i + 2] for i in range(len(token) - 1))
    return terms


def _section_relevance(section: str, query: str) -> float:
    """小节与查询的相关性：命中词元占比（0-1+）。"""
    terms = _query_terms(query)
    if not terms or not section:
        return 0.0
    text = section.lower()
    hits = sum(1 for term in terms if term in text)
    return hits / len(terms)


def _split_memory_sections(content: str) -> list[str]:
    """按空行拆分记忆小节，过滤空白。"""
    return [s.strip() for s in str(content or "").split("\n\n") if s.strip()]


def describe_memory_path(path: str) -> dict:
    path = str(path or "")
    category = "legacy"
    if path.endswith("/user_persona.md"):
        category = "user_persona"
    elif path.endswith("/character_persona.md"):
        category = "character_persona"
    elif path.endswith("/recent_digest.md"):
        category = "recent_digest"
    elif path.endswith("/timeline.md"):
        # 跨会话时间线：仅顶层 characters/{char_id}/timeline.md
        category = "timeline"
    elif "/life_sim/" in path:
        # 角色生活片段：characters/{char_id}/life_sim/{conversation_id}.md
        # （注意：必须先于 /events/ /plot/ 判断，避免 /life_sim/ 被当成 events）
        category = "life_sim"
    elif "/events/" in path or "/plot/" in path:
        category = "important_event"

    meta = _MEMORY_CATEGORY_META[category]
    return {
        "category": category,
        "category_label": meta["label"],
        "injects_to_prompt": bool(meta["injects_to_prompt"]),
        "category_order": meta["order"],
    }


class MemoryFS:
    """记忆逻辑文件系统

    路径规范：
        characters/{char_id}/general.md           角色通用信息
        characters/{char_id}/users/{user_id}/user_persona.md       用户人格/偏好画像
        characters/{char_id}/users/{user_id}/character_persona.md  角色对用户的关系理解
        characters/{char_id}/users/{user_id}/recent_digest.md      压缩后的近期摘要
        characters/{char_id}/events/{conversation_id}.md           重要事件
        characters/{char_id}/users/{user_id}.md                    旧版用户关系摘要
        characters/{char_id}/diary/daily.md                        旧版日记路径
        characters/{char_id}/diary/weekly.md       本周摘要
        characters/{char_id}/plot/{conv_id}.md    剧情摘要
        characters/{char_id}/world/events.md       世界事件记录
        characters/{char_id}/timeline.md           跨会话人生经历时间线（按时间顺序累积）
    """

    def __init__(self, data_dir: str = _DEFAULT_DATA_DIR):
        self._index_file = os.path.join(data_dir, _FS_INDEX_FILE)
        self._index: dict[str, MemoryFile] = {}
        self._load()

    # ------------------------------------------------------------------
    # 核心接口
    # ------------------------------------------------------------------

    def read(self, path: str) -> MemoryFile | None:
        """读取逻辑路径对应的记忆文件。"""
        return self._index.get(path)

    def write(
        self,
        path: str,
        *,
        character_id: str = "",
        target_id: str = "",
        title: str = "",
        content: str = "",
        summary: str = "",
        tags: list[str] | None = None,
        importance: float = 0.0,
        source_event_id: str = "",
        memory_ids: list[str] | None = None,
        append: bool = False,
    ) -> MemoryFile:
        """写入或更新逻辑路径的记忆文件。

        append=True 时将 content 追加到现有内容末尾（用于日记）。
        """
        existing = self._index.get(path)

        if existing and append:
            new_content = existing.content + "\n\n" + content if existing.content else content
            # 截断：防止无限膨胀（diary/plot/life_sim 按条目数，其他按字符数）
            needs_truncation = (
                ("diary" in path and new_content.count("\n\n") >= _MAX_DIARY_ENTRIES)
                or ("plot" in path and new_content.count("\n\n") >= _MAX_PLOT_ENTRIES)
                or ("life_sim" in path and new_content.count("\n\n") >= _MAX_LIFE_SIM_ENTRIES)
                or len(new_content) > _MAX_MEMORY_FILE_CHARS
            )
            if needs_truncation:
                new_content = _truncate_entries(new_content, path)
            new_ids = list(set((existing.memory_ids or []) + (memory_ids or [])))
            mf = MemoryFile(
                path=path,
                character_id=character_id or existing.character_id,
                target_id=target_id or existing.target_id,
                title=title or existing.title,
                content=new_content,
                summary=summary or existing.summary,
                tags=tags if tags is not None else existing.tags,
                importance=max(importance, existing.importance),
                version=existing.version + 1,
                source_event_id=source_event_id or existing.source_event_id,
                memory_ids=new_ids,
            )
        elif existing:
            mf = MemoryFile(
                path=path,
                character_id=character_id or existing.character_id,
                target_id=target_id or existing.target_id,
                title=title or existing.title,
                content=content if content else existing.content,
                summary=summary if summary else existing.summary,
                tags=tags if tags is not None else existing.tags,
                importance=importance if importance > 0 else existing.importance,
                version=existing.version + 1,
                source_event_id=source_event_id or existing.source_event_id,
                memory_ids=list(set((existing.memory_ids or []) + (memory_ids or []))),
            )
        else:
            mf = MemoryFile(
                path=path,
                character_id=character_id,
                target_id=target_id,
                title=title,
                content=content,
                summary=summary,
                tags=tags or [],
                importance=importance,
                source_event_id=source_event_id,
                memory_ids=memory_ids or [],
            )

        # 最终截断保护：确保任何路径都不超过字符上限（按条目截断，不切断单条记忆）
        if len(mf.content) > _MAX_MEMORY_FILE_CHARS:
            truncated = _truncate_entries(mf.content, path)
            mf = MemoryFile(
                path=mf.path,
                character_id=mf.character_id,
                target_id=mf.target_id,
                title=mf.title,
                content=truncated,
                summary=mf.summary,
                tags=mf.tags,
                importance=mf.importance,
                version=mf.version,
                source_event_id=mf.source_event_id,
                memory_ids=mf.memory_ids,
            )

        self._index[path] = mf
        self._save()
        _log.debug("[MemoryFS] write path=%s version=%d", path, mf.version)
        return mf

    def delete(self, path: str) -> bool:
        if path in self._index:
            del self._index[path]
            self._save()
            return True
        return False

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------

    def path_user(self, char_id: str, user_id: str) -> str:
        return f"characters/{char_id}/users/{user_id}.md"

    def path_user_persona(self, char_id: str, user_id: str) -> str:
        return f"characters/{char_id}/users/{user_id}/user_persona.md"

    def path_character_persona(self, char_id: str, user_id: str) -> str:
        return f"characters/{char_id}/users/{user_id}/character_persona.md"

    def path_recent_digest(self, char_id: str, user_id: str) -> str:
        return f"characters/{char_id}/users/{user_id}/recent_digest.md"

    def path_important_events(self, char_id: str, conversation_id: str) -> str:
        safe_conversation_id = conversation_id or "general"
        return f"characters/{char_id}/events/{safe_conversation_id}.md"

    def path_diary_daily(self, char_id: str) -> str:
        return f"characters/{char_id}/diary/daily.md"

    def path_diary_weekly(self, char_id: str) -> str:
        return f"characters/{char_id}/diary/weekly.md"

    def path_plot(self, char_id: str, conversation_id: str) -> str:
        return f"characters/{char_id}/plot/{conversation_id}.md"

    def path_world_events(self, char_id: str) -> str:
        return f"characters/{char_id}/world/events.md"

    def path_life_sim(self, char_id: str, conversation_id: str) -> str:
        """静默心跳生成的角色生活片段。按 conversation_id 隔离，
        不会跨会话串数据，也不会污染跨会话 timeline。
        """
        safe_conversation_id = conversation_id or "general"
        return f"characters/{char_id}/life_sim/{safe_conversation_id}.md"

    def path_timeline(self, char_id: str) -> str:
        return f"characters/{char_id}/timeline.md"

    def path_general(self, char_id: str) -> str:
        return f"characters/{char_id}/general.md"

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_for_character(self, char_id: str) -> list[MemoryFile]:
        """列出指定角色的所有逻辑文件，按 importance 降序。"""
        prefix = f"characters/{char_id}/"
        files = [mf for path, mf in self._index.items() if path.startswith(prefix)]
        return sorted(files, key=lambda m: m.importance, reverse=True)

    # ------------------------------------------------------------------
    # 公开列表接口（供 Web 面板 / 全局搜索 / 导出等只读使用）
    # ------------------------------------------------------------------

    def list_paths(self, prefix: str = "") -> list[str]:
        """列出所有逻辑路径（可按前缀过滤），按字母序稳定输出。"""
        prefix = prefix or ""
        return sorted(path for path in self._index if path.startswith(prefix))

    def list_all(
        self,
        prefix: str = "",
        *,
        character_id: str = "",
        target_id: str = "",
        tags: list[str] | None = None,
        order_by: str = "path",
        descending: bool = False,
    ) -> list[MemoryFile]:
        """列出全部逻辑记忆文件，可按前缀 / 角色 / 关联对象 / 标签过滤。

        Args:
            prefix: 路径前缀，例如 ``characters/neko/``
            character_id: 只保留该角色的文件
            target_id: 只保留关联到该用户/角色的文件
            tags: 只保留含任一标签的文件（空列表视为不过滤）
            order_by: ``path`` / ``updated_at`` / ``importance`` / ``title``
            descending: 是否倒序（``updated_at`` 常用倒序取最新）
        """
        files = list(self.iter_files(
            prefix,
            character_id=character_id,
            target_id=target_id,
            tags=tags,
        ))
        key = {
            "path": lambda item: item.path,
            "updated_at": lambda item: item.updated_at or "",
            "importance": lambda item: float(item.importance or 0.0),
            "title": lambda item: item.title or item.path,
        }.get(order_by, lambda item: item.path)
        return sorted(files, key=key, reverse=bool(descending))

    def iter_files(
        self,
        prefix: str = "",
        *,
        character_id: str = "",
        target_id: str = "",
        tags: list[str] | None = None,
    ) -> Iterator[MemoryFile]:
        """迭代符合条件的逻辑记忆文件（过滤逻辑与 :meth:`list_all` 一致）。"""
        prefix = prefix or ""
        wanted_tags = {str(tag).strip() for tag in (tags or []) if str(tag).strip()}
        for path, memory_file in self._index.items():
            if prefix and not path.startswith(prefix):
                continue
            if character_id and memory_file.character_id != character_id:
                continue
            if target_id and memory_file.target_id != target_id:
                continue
            if wanted_tags and not wanted_tags.intersection(memory_file.tags or []):
                continue
            yield memory_file

    def stats(self) -> dict[str, Any]:
        """索引概览：文件数 / 总字数 / 角色数，供面板与全局搜索使用。"""
        files = list(self._index.values())
        return {
            "files": len(files),
            "chars": sum(len(mf.content or "") for mf in files),
            "characters": len({
                mf.character_id for mf in files if mf.character_id
            }),
            "index_file": self._index_file,
        }

    def read_user(self, char_id: str, user_id: str) -> MemoryFile | None:
        return self.read(self.path_user(char_id, user_id))

    def read_diary(self, char_id: str) -> MemoryFile | None:
        return self.read(self.path_diary_daily(char_id))

    def read_plot(self, char_id: str, conversation_id: str) -> MemoryFile | None:
        return self.read(self.path_plot(char_id, conversation_id))

    def build_prompt_context(
        self,
        char_id: str,
        user_id: str,
        conversation_id: str = "",
        query: str = "",
        max_chars: int = 0,
    ) -> str:
        """按结构化读取策略构建 prompt 注入文本（必读层）。

        必读：用户人格 + 角色关系理解 + 跨会话时间线 + 重要事件/剧情摘要 + 压缩近期摘要。
        旧版 raw daily 不再注入，避免把逐轮流水账塞进上下文。
        timeline 为跨会话人生经历汇总，注入最近 N 条，替代按 conversation 切片的事件注入。

        检索化注入：提供 query 时，各记忆文件按"相关性 × 重要度 × 时间衰减"
        挑选小节填充 max_chars 预算（人格类小节优先保留）；max_chars<=0 或
        无 query 时保持整段注入的旧行为。
        """
        parts: list[str] = []

        if max_chars <= 0 or not query:
            return self._build_prompt_context_full(char_id, user_id, conversation_id)

        from datetime import datetime as _dt

        def _decay(updated_at: str) -> float:
            try:
                ts = _dt.fromisoformat((updated_at or "").replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.astimezone()
                days = abs((_dt.now().astimezone() - ts).total_seconds()) / 86400
            except (ValueError, TypeError):
                return 0.5
            if days < 3:
                return 1.0
            if days < 14:
                return 0.7
            return 0.4

        candidates: list[tuple[float, bool, str]] = []  # (score, must_read, text)

        def _add_sections(mf, *, label: str, must_read: bool = False, weight: float = 1.0):
            if not mf or not mf.content:
                return
            sections = _split_memory_sections(mf.content)
            if not sections:
                return
            limit = 3 if must_read else 2
            scored = sorted(
                (
                    (
                        _section_relevance(section, query) * 2.0
                        + float(mf.importance or 0.0) * weight
                        + _decay(mf.updated_at)
                    ),
                    section,
                )
                for section in sections
            )
            top = sorted(scored, reverse=True)[:limit]
            for score, section in top:
                text = f"[{label}]\n{section.strip()}"
                candidates.append((score * weight, must_read, text))

        _add_sections(
            self.read(self.path_user_persona(char_id, user_id)),
            label="user_persona", must_read=True, weight=1.2,
        )
        _add_sections(
            self.read(self.path_character_persona(char_id, user_id)),
            label="character_persona", must_read=True, weight=1.1,
        )

        timeline_mf = self.read(self.path_timeline(char_id))
        if timeline_mf and timeline_mf.content:
            timeline_text = format_timeline_for_prompt(
                timeline_mf.content,
                current_conversation_id=conversation_id,
            )
            if timeline_text:
                candidates.append((
                    0.6 * float(timeline_mf.importance or 0.5) * _decay(timeline_mf.updated_at) + _section_relevance(timeline_text, query),
                    False,
                    f"[character.timeline]\n{timeline_text}",
                ))

        if conversation_id:
            event_mf = self.read(self.path_important_events(char_id, conversation_id))
            _add_sections(event_mf, label="important_event", weight=1.0)
            plot_mf = self.read_plot(char_id, conversation_id)
            _add_sections(plot_mf, label="plot", weight=0.9)
            life_sim_mf = self.read(self.path_life_sim(char_id, conversation_id))
            if life_sim_mf and life_sim_mf.content:
                life_sim_text = format_life_sim_for_prompt(life_sim_mf.content)
                if life_sim_text:
                    candidates.append((
                        0.4 * float(life_sim_mf.importance or 0.4) * _decay(life_sim_mf.updated_at),
                        False,
                        f"[character.life_sim]\n{life_sim_text}",
                    ))

        _add_sections(
            self.read(self.path_recent_digest(char_id, user_id)),
            label="recent_digest", weight=0.8,
        )

        # 填充预算：必读小节优先，其余按分数降序
        ordered = sorted(candidates, key=lambda c: (not c[1], -c[0]))
        used = 0
        for _score, _must, text in ordered:
            if used + len(text) > max_chars:
                continue
            parts.append(text)
            used += len(text)
            if used >= max_chars:
                break

        return "\n\n".join(p for p in parts if p)

    def _build_prompt_context_full(
        self,
        char_id: str,
        user_id: str,
        conversation_id: str = "",
    ) -> str:
        """整段注入（旧行为）：无预算/无查询时的兼容路径。"""
        parts: list[str] = []

        user_persona = self.read(self.path_user_persona(char_id, user_id))
        if user_persona:
            parts.append(user_persona.to_prompt_text())

        character_persona = self.read(self.path_character_persona(char_id, user_id))
        if character_persona:
            parts.append(character_persona.to_prompt_text())

        timeline_mf = self.read(self.path_timeline(char_id))
        if timeline_mf and timeline_mf.content:
            # 按会话分桶：每桶最新 2 条，全局最多 10 条，
            # 排除当前会话（避免与 events/{conv_id} 重复）。
            timeline_text = format_timeline_for_prompt(
                timeline_mf.content,
                current_conversation_id=conversation_id,
            )
            if timeline_text:
                # 与 MemoryFile.to_prompt_text() 的 [{title or path}] 风格保持一致
                parts.append(f"[character.timeline]\n{timeline_text}")

        if conversation_id:
            event_mf = self.read(self.path_important_events(char_id, conversation_id))
            if event_mf:
                parts.append(event_mf.to_prompt_text())
            plot_mf = self.read_plot(char_id, conversation_id)
            if plot_mf:
                parts.append(plot_mf.to_prompt_text())
            # 静默心跳生成的"本会话生活片段"——按 conversation_id 严格隔离
            # 注入时调用 format_life_sim_for_prompt，限制最新 _MAX_LIFE_SIM_INJECT 条
            # 并加 header 说明，避免与 events/plot 重复/混淆
            life_sim_mf = self.read(self.path_life_sim(char_id, conversation_id))
            if life_sim_mf and life_sim_mf.content:
                life_sim_text = format_life_sim_for_prompt(life_sim_mf.content)
                if life_sim_text:
                    # 与 MemoryFile.to_prompt_text() 的 [{title or path}] 风格保持一致
                    parts.append(f"[character.life_sim]\n{life_sim_text}")

        digest_mf = self.read(self.path_recent_digest(char_id, user_id))
        if digest_mf:
            parts.append(digest_mf.to_prompt_text())

        return "\n\n".join(p for p in parts if p)

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self._index_file):
            self._index = {}
            return
        try:
            with open(self._index_file, encoding="utf-8") as f:
                raw = json.load(f)
            self._index = {k: MemoryFile.from_dict(v) for k, v in raw.items()}
            _log.debug("[MemoryFS] loaded %d entries from %s", len(self._index), self._index_file)
        except Exception as exc:
            _log.warning("[MemoryFS] load failed: %s", exc)
            self._index = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._index_file), exist_ok=True)
            with open(self._index_file, "w", encoding="utf-8") as f:
                json.dump(
                    {k: v.to_dict() for k, v in self._index.items()},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as exc:
            _log.error("[MemoryFS] save failed: %s", exc)


def get_memory_fs(data_dir: str = _DEFAULT_DATA_DIR) -> MemoryFS:
    """获取全局 MemoryFS 单例。"""
    global _memory_fs
    if _memory_fs is None:
        _memory_fs = MemoryFS(data_dir=data_dir)
    return _memory_fs
