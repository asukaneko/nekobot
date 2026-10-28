"""
世界书 JSON 存储层

管理世界书及其条目的持久化存储，
复用 JsonStore 的线程安全读写机制。
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from nbot.character.models import WorldBook, WorldBookEntry
from nbot.character.storage.json_store import JsonStore

_log = logging.getLogger(__name__)


class WorldBookStore:
    """世界书存储管理器"""

    def __init__(self, base_dir: str):
        self._store = JsonStore(os.path.join(base_dir, "data", "world_books.json"))

    def _load_books(self) -> Dict[str, Any]:
        data = self._store.get("world_books")
        return data if isinstance(data, dict) else {}

    def _save_books(self, books: Dict[str, Any]) -> None:
        self._store.set("world_books", books)

    # ---- 世界书 CRUD ----

    def list_all(self) -> List[WorldBook]:
        """列出所有世界书"""
        books_data = self._load_books()
        return [WorldBook.from_dict(self._normalize_book_data(v)) for v in books_data.values()]

    def get(self, book_id: str) -> Optional[WorldBook]:
        """获取单个世界书"""
        books_data = self._load_books()
        data = books_data.get(book_id)
        if data and isinstance(data, dict):
            data = self._normalize_book_data(data)
            return WorldBook.from_dict(data)
        return None

    @staticmethod
    def _normalize_book_data(data: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize entries from list or dict format to list for WorldBook.from_dict."""
        raw = data.get("entries", {})
        if isinstance(raw, dict):
            data["entries"] = list(raw.values())
        elif not isinstance(raw, list):
            data["entries"] = []
        return data

    def create(
        self,
        name: str,
        description: str = "",
        cover_url: str = "",
        character_ids: Optional[List[str]] = None,
    ) -> WorldBook:
        """创建世界书"""
        now = datetime.now().isoformat()
        book = WorldBook(
            id=uuid.uuid4().hex[:12],
            name=name,
            description=description,
            cover_url=cover_url,
            character_ids=character_ids or [],
            entries=[],
            enabled=True,
            created_at=now,
            updated_at=now,
        )

        books_data = self._load_books()
        books_data[book.id] = book.to_dict()
        self._save_books(books_data)
        return book

    def update(self, book_id: str, **kwargs) -> Optional[WorldBook]:
        """更新世界书元信息"""
        books_data = self._load_books()
        if not isinstance(books_data, dict):
            _log.warning("[WorldBookStore] books_data is %s, resetting", type(books_data).__name__)
            books_data = {}

        data = books_data.get(book_id)
        if not data or not isinstance(data, dict):
            return None

        for key in ("name", "description", "cover_url", "character_ids", "enabled"):
            if key in kwargs:
                data[key] = kwargs[key]
        data["updated_at"] = datetime.now().isoformat()

        books_data[book_id] = data
        self._save_books(books_data)
        return WorldBook.from_dict(self._normalize_book_data(data))

    def delete(self, book_id: str) -> bool:
        """删除世界书"""
        books_data = self._load_books()
        if not isinstance(books_data, dict):
            return False
        if book_id in books_data:
            del books_data[book_id]
            self._save_books(books_data)
            return True
        return False

    # ---- 条目 CRUD ----

    def list_entries(self, book_id: str) -> List[WorldBookEntry]:
        """列出世界书的所有条目"""
        book = self.get(book_id)
        return book.entries if book else []

    def get_entry(self, book_id: str, entry_id: str) -> Optional[WorldBookEntry]:
        """获取单个条目"""
        entries = self.list_entries(book_id)
        for entry in entries:
            if entry.id == entry_id:
                return entry
        return None

    def add_entry(self, book_id: str, entry_data: Dict[str, Any]) -> Optional[WorldBookEntry]:
        """添加条目"""
        books_data = self._load_books()
        data = books_data.get(book_id)
        if not data:
            return None

        now = datetime.now().isoformat()
        entry = WorldBookEntry(
            id=uuid.uuid4().hex[:12],
            name=entry_data.get("name", ""),
            keywords=entry_data.get("keywords", []),
            content=entry_data.get("content", ""),
            enabled=entry_data.get("enabled", True),
            priority=entry_data.get("priority", 0),
            case_sensitive=entry_data.get("case_sensitive", False),
            match_mode=entry_data.get("match_mode", "any"),
            trigger_sources=entry_data.get("trigger_sources", ["user"]),
            always_on=entry_data.get("always_on", False),
            state_triggers=entry_data.get("state_triggers", {}),
            cooldown_turns=entry_data.get("cooldown_turns", 0),
            max_injections_per_session=entry_data.get("max_injections_per_session", 0),
            tags=entry_data.get("tags", []),
            entry_type=entry_data.get("entry_type", "lore"),
            weight=entry_data.get("weight", 0),
            position=entry_data.get("position", ""),
            depth=entry_data.get("depth", 4),
            probability=entry_data.get("probability", 100),
            use_regex=entry_data.get("use_regex", False),
            match_whole_words=entry_data.get("match_whole_words", False),
            created_at=now,
            updated_at=now,
        )

        raw_entries = data.get("entries", {})
        entries = dict(raw_entries) if isinstance(raw_entries, dict) else {}
        entries[entry.id] = entry.to_dict()
        data["entries"] = entries
        data["updated_at"] = now
        books_data[book_id] = data
        self._save_books(books_data)
        return entry

    def update_entry(
        self, book_id: str, entry_id: str, **kwargs
    ) -> Optional[WorldBookEntry]:
        """更新条目"""
        books_data = self._load_books()
        data = books_data.get(book_id)
        if not data or not isinstance(data, dict):
            return None

        raw_entries = data.get("entries", {})
        entries = dict(raw_entries) if isinstance(raw_entries, dict) else {}
        entry_data = entries.get(entry_id)
        if not entry_data:
            return None

        for key in ("name", "keywords", "content", "enabled", "priority", "case_sensitive", "match_mode",
                    "trigger_sources", "always_on", "state_triggers", "cooldown_turns",
                    "max_injections_per_session", "tags", "entry_type", "weight",
                    "position", "depth", "probability", "use_regex", "match_whole_words"):
            if key in kwargs:
                entry_data[key] = kwargs[key]
        entry_data["updated_at"] = datetime.now().isoformat()

        entries[entry_id] = entry_data
        data["entries"] = entries
        data["updated_at"] = datetime.now().isoformat()
        books_data[book_id] = data
        self._save_books(books_data)
        return WorldBookEntry.from_dict(entry_data)

    def delete_entry(self, book_id: str, entry_id: str) -> bool:
        """删除条目"""
        books_data = self._load_books()
        data = books_data.get(book_id)
        if not data or not isinstance(data, dict):
            return False

        raw_entries = data.get("entries", {})
        entries = dict(raw_entries) if isinstance(raw_entries, dict) else {}
        if entry_id in entries:
            del entries[entry_id]
            data["entries"] = entries
            data["updated_at"] = datetime.now().isoformat()
            books_data[book_id] = data
            self._save_books(books_data)
            return True
        return False

    def batch_add_entries(
        self, book_id: str, entries_data: List[Dict[str, Any]]
    ) -> List[WorldBookEntry]:
        """批量添加条目"""
        results = []
        for entry_data in entries_data:
            entry = self.add_entry(book_id, entry_data)
            if entry:
                results.append(entry)
        return results
