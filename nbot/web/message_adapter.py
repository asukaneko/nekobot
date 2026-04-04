import json
import logging
import os
import re
import shutil
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING

from nbot.channels import WebChannelAdapter
from nbot.core import ChatResponse, WebSessionStore

_log = logging.getLogger(__name__)

try:
    from nbot.core.knowledge import get_knowledge_manager

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    KNOWLEDGE_MANAGER_AVAILABLE = False

try:
    from nbot.core.prompt import prompt_manager

    PROMPT_MANAGER_AVAILABLE = True
except ImportError:
    PROMPT_MANAGER_AVAILABLE = False
    prompt_manager = None

if TYPE_CHECKING:
    from nbot.web.server import WebChatServer


class WebMessageAdapter:
    """Adapt Web messages to the QQ-style command/chat flow."""

    def __init__(
        self, content: str, user_id: str, session_id: str, server: "WebChatServer"
    ):
        self.raw_message = content
        self.user_id = user_id
        self.group_id = None
        self.session_id = session_id
        self.server = server
        self._reply_text = None
        self._reply_image = None
        self._thinking_card_id = None
        self.session_store = WebSessionStore(
            self.server.sessions, save_callback=lambda: self.server._save_data("sessions")
        )
        self.channel_adapter = getattr(self.server, "web_channel_adapter", None) or WebChannelAdapter()

        try:
            from nbot.commands import admin

            if str(user_id) not in admin:
                admin.append(str(user_id))
        except ImportError:
            pass

        self.api = self._create_mock_api()
        self.bot = SimpleNamespace(api=self.api)

    def _load_memories_for_context(self, user_id: str) -> str:
        if not user_id:
            return ""

        if PROMPT_MANAGER_AVAILABLE and prompt_manager:
            try:
                memory_text = prompt_manager.load_memories(user_id)
                if memory_text:
                    return memory_text
            except Exception as e:
                _log.error(f"Failed to load memories from prompt_manager: {e}")

        memories = []
        now = datetime.now()

        for mem in getattr(self, "memories", []):
            mem_target = mem.get("target_id", "")
            if mem_target and mem_target != user_id:
                continue

            mem_type = mem.get("type", "long")
            title = mem.get("title", mem.get("key", ""))
            content = mem.get("content", mem.get("value", ""))
            summary = mem.get("summary", "")

            if mem_type == "short":
                created_at = mem.get("created_at", "")
                expire_days = mem.get("expire_days", 7)
                if created_at:
                    try:
                        created = datetime.fromisoformat(created_at)
                        diff_days = (now - created).days
                        if diff_days > expire_days:
                            continue
                    except Exception:
                        pass

            if title or content:
                display_text = (
                    summary
                    if summary
                    else (content[:100] + "..." if len(content) > 100 else content)
                )
                memories.append(f"[{title}] {display_text}")

        if memories:
            return "\n".join(["[Memories]"] + memories)
        return ""

    def _retrieve_knowledge(self, query: str, max_docs: int = 3) -> str:
        if not KNOWLEDGE_MANAGER_AVAILABLE or not query:
            return ""

        try:
            km = get_knowledge_manager()
            if not km:
                return ""

            results = km.search(query, base_id=None, top_k=max_docs)
            if not results or all(sim < 0.1 for _, sim, _ in results):
                _log.info("[Knowledge] fallback to keyword search")
                results = self._keyword_search(km, query, max_docs)

            if not results:
                return ""

            knowledge_parts = ["[Knowledge]"]
            seen_titles = set()

            for doc, similarity, chunk_content in results:
                if doc.title in seen_titles:
                    continue
                seen_titles.add(doc.title)
                content = chunk_content[:500] + ("..." if len(chunk_content) > 500 else "")
                knowledge_parts.append(f"\n# {doc.title}\n{content}")

            return "\n".join(knowledge_parts) if seen_titles else ""
        except Exception as e:
            _log.error(f"[Knowledge] retrieval failed: {e}")
            return ""

    def _keyword_search(self, km, query: str, max_docs: int = 3) -> list:
        try:
            bases = km.list_knowledge_bases()
            if not bases:
                return []

            query_words = set(re.findall(r"[\w]+", query.lower()))
            all_docs = []
            for kb in bases:
                for doc_id in kb.documents:
                    doc = km.store.load_document(doc_id)
                    if doc:
                        all_docs.append((doc, doc.content))

            scored = []
            for doc, content in all_docs:
                content_lower = content.lower()
                title_lower = doc.title.lower()
                score = 0
                for word in query_words:
                    if word in title_lower:
                        score += 3
                    if word in content_lower:
                        score += 1
                if score > 0:
                    scored.append((doc, score, content))

            scored.sort(key=lambda x: x[1], reverse=True)
            return [(doc, float(score), content) for doc, score, content in scored[:max_docs]]
        except Exception as e:
            _log.error(f"[Knowledge] keyword search failed: {e}")
            return []

    def _create_mock_api(self):
        adapter = self

        class MockAPI:
            async def post_group_file(self, group_id, file=None, **kwargs):
                if file:
                    return await adapter.send_file(file)
                return True

            async def upload_private_file(self, user_id, file=None, name=None, **kwargs):
                if file:
                    return await adapter.send_file(file, name)
                return True

            async def post_private_file(self, user_id, file=None, name=None, **kwargs):
                if file:
                    file_name = name if name else os.path.basename(file)
                    return await adapter.send_file(file, file_name)
                return True

            async def post_private_msg(self, user_id, text=None, rtf=None, **kwargs):
                if kwargs.get("dice"):
                    import random

                    text = f"Dice: {random.randint(1, 6)}"
                elif kwargs.get("rps"):
                    import random

                    text = f"RPS: {random.choice(['rock', 'paper', 'scissors'])}"
                elif rtf:
                    text = str(rtf) if hasattr(rtf, "__str__") else str(rtf)
                elif not text:
                    text = ""
                return await adapter.reply(text=text)

            async def post_group_msg(self, group_id, text=None, rtf=None, **kwargs):
                return await self.post_private_msg(None, text=text, rtf=rtf, **kwargs)

            async def set_friend_add_request(
                self, flag, approve=True, remark=None, **kwargs
            ):
                _log.info(
                    f"Mock friend request response: flag={flag}, approve={approve}, remark={remark}"
                )
                return True

        return MockAPI()

    async def reply(self, text: str = None, image: str = None, rtf=None):
        if rtf is not None:
            content_text = ""
            if isinstance(rtf, list):
                for item in rtf:
                    if isinstance(item, str):
                        content_text += item
                    elif hasattr(item, "text"):
                        content_text += item.text
            elif hasattr(rtf, "text"):
                content_text = rtf.text
            elif hasattr(rtf, "__str__"):
                content_text = str(rtf)
            if content_text:
                text = content_text

        if text:
            self._reply_text = text
            message = self.channel_adapter.build_assistant_message(
                ChatResponse(final_content=text),
                conversation_id=self.session_id,
                sender="AI",
            )
            if self.session_id in self.server.sessions:
                self.session_store.append_message(self.session_id, message)
            self.server.socketio.emit("new_message", message, room=self.session_id)
        if image:
            self._reply_image = image

    async def send_file(self, file_path: str, file_name: str = None):
        import base64
        import hashlib
        import mimetypes
        import time

        if not os.path.exists(file_path):
            _log.error(f"File not found: {file_path}")
            return False

        if not file_name:
            file_name = os.path.basename(file_path)

        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "application/octet-stream"

        ext = os.path.splitext(file_path)[1].lower()

        try:
            file_size = os.path.getsize(file_path)
        except Exception as e:
            _log.error(f"Failed to read file size for {file_path}: {e}")
            return False

        is_image = mime_type.startswith("image/")
        is_text = mime_type.startswith("text/") or mime_type in [
            "application/json",
            "application/xml",
            "application/yaml",
        ]
        is_video = mime_type.startswith("video/")
        is_audio = mime_type.startswith("audio/")

        files_dir = os.path.join(self.server.static_folder, "files")
        os.makedirs(files_dir, exist_ok=True)

        file_hash = hashlib.md5(f"{file_path}{time.time()}".encode()).hexdigest()[:8]
        safe_name = f"{file_hash}_{file_name}"
        dest_path = os.path.join(files_dir, safe_name)
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        try:
            shutil.copy2(file_path, dest_path)
            _log.info(f"Copied file to web static dir: {dest_path}")

            cache_dirs = [
                os.path.join(self.server.base_dir, "data", "cache"),
                os.path.join(self.server.base_dir, "data", "workspace"),
                os.path.join(self.server.base_dir, "rank"),
                os.path.join(self.server.base_dir, "search"),
            ]
            is_cache_file = any(file_path.startswith(cache_dir) for cache_dir in cache_dirs)
            if is_cache_file and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                    _log.info(f"Deleted temp file: {file_path}")
                except Exception as del_err:
                    _log.warning(f"Failed to delete temp file: {del_err}")
        except Exception as e:
            _log.error(f"Failed to copy file for web delivery: {e}")
            return False

        if getattr(self.server, "WORKSPACE_AVAILABLE", False):
            try:
                session = self.session_store.get_session(self.session_id) or {}
                session_type = session.get("type", "web")
                self.server.workspace_manager.register_file_reference(
                    self.session_id,
                    dest_path,
                    file_name,
                    session_type=session_type,
                    metadata={
                        "download_url": f"/static/files/{safe_name}",
                        "source": "web_message_adapter",
                    },
                )
            except Exception as ref_err:
                _log.warning(f"Failed to register workspace file reference: {ref_err}")

        download_url = f"/static/files/{safe_name}"
        file_info = self.channel_adapter.build_assistant_message(
            ChatResponse(final_content=f"[File: {file_name}]"),
            conversation_id=self.session_id,
            sender="AI",
            metadata={
                "file": {
                    "name": file_name,
                    "type": mime_type,
                    "size": file_size,
                    "is_image": is_image,
                    "is_text": is_text,
                    "is_video": is_video,
                    "is_audio": is_audio,
                    "extension": ext,
                    "download_url": download_url,
                    "url": download_url,
                    "safe_name": safe_name,
                }
            },
        )

        if is_image and file_size < 5 * 1024 * 1024:
            try:
                with open(file_path, "rb") as f:
                    file_data = f.read()
                b64_data = base64.b64encode(file_data).decode("utf-8")
                file_info["file"]["data"] = f"data:{mime_type};base64,{b64_data}"
                file_info["file"]["preview_url"] = file_info["file"]["data"]
            except Exception as e:
                _log.error(f"Failed to inline image as base64: {e}")
        elif is_text and file_size < 102400:
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    file_info["file"]["content"] = f.read()[:5000]
            except Exception as e:
                _log.warning(f"Failed to read text preview: {e}")

        if self.session_id in self.server.sessions:
            self.session_store.append_message(self.session_id, file_info)

        self.server.socketio.emit("new_message", file_info, room=self.session_id)
        _log.info(f"Sent file to web session: {file_name} ({mime_type}, {file_size} bytes)")
        return True
