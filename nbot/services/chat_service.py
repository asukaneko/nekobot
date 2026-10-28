import datetime
import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from nbot.channels.qq import QQChannelAdapter
from nbot.channels.registry import get_channel_adapter, register_channel_handler
from nbot.core import (
    AgentService,
    ChatRequest,
    ChatResponse,
    QQSessionStore,
    build_qq_session_id,
    clean_response_content,
    dump_json,
    extract_display_text,
    message_manager,
)
# 注意：必须从定义处导入 prompt_manager 实例。`nbot.core` 包里的同名导出会被
# 子模块 nbot.core.prompt_manager 覆盖，从包里取到的可能是模块而非 PromptManager。
from nbot.core.prompt import prompt_manager
from nbot.core.ai_pipeline import (
    AIPipeline,
    ModePolicy,
    PipelineCallbacks,
    PipelineContext,
    PipelineResult,
    handle_tool_confirmation,
)
from nbot.core.message import create_message
from nbot.services.ai import (
    MAX_HISTORY_LENGTH,
    ai_client,
    group_messages,
    refresh_runtime_ai_config,
    user_messages,
)

# 工作区管理
try:
    from nbot.core.workspace import workspace_manager
    WORKSPACE_AVAILABLE = True
except ImportError:
    workspace_manager = None
    WORKSPACE_AVAILABLE = False

# 工具调用支持
try:
    from nbot.services.tools import (
        _CONFIRM_KEYWORDS,
        _REJECT_KEYWORDS,
        TOOL_DEFINITIONS,
        execute_pending_command,
        execute_tool,
        get_pending_by_session,
        reject_pending_command,
    )
    TOOLS_AVAILABLE = True
except ImportError:
    TOOL_DEFINITIONS = []
    execute_tool = None
    get_pending_by_session = None
    execute_pending_command = None
    reject_pending_command = None
    _CONFIRM_KEYWORDS = set()
    _REJECT_KEYWORDS = set()
    TOOLS_AVAILABLE = False

# 工具执行线程池（避免阻塞主线程）
_tool_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="tool_exec")

# 知识库管理
try:
    from nbot.core.knowledge import get_knowledge_manager
    KNOWLEDGE_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    KNOWLEDGE_AVAILABLE = False

last_log_entry = {}
_log = logging.getLogger(__name__)

# 群聊 @mention 跨角色对话消息队列
# _run_qq_chat_request 产生跨角色对话后存入此队列，
# 由 commands.py 在发送主回复后逐条发送
_cross_talk_queue: list[dict[str, str]] = []


def pop_cross_talk_messages() -> list[dict[str, str]]:
    """取出并清空跨角色对话消息队列。

    Returns:
        消息列表，每条含 {speaker_name, content}
    """
    global _cross_talk_queue
    messages = list(_cross_talk_queue)
    _cross_talk_queue = []
    return messages


def _get_project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_json_file(path: str, default=None):
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[QQ Character] failed to load {path}: {e}")
    return default


def _load_resume_bindings() -> dict:
    path = os.path.join(_get_project_root(), "data", "web", "qq_web_session_bindings.json")
    data = _load_json_file(path, {})
    return data if isinstance(data, dict) else {}


def _sync_bound_web_session(server, qq_session_id: str) -> None:
    if not server or not qq_session_id:
        return

    bindings = _load_resume_bindings()
    binding = bindings.get(qq_session_id)
    if not isinstance(binding, dict):
        return

    web_session_id = str(binding.get("web_session_id") or "").strip()
    if not web_session_id:
        return

    qq_session = server.session_store.get_session(qq_session_id)
    web_session = server.session_store.get_session(web_session_id)
    if not qq_session or not web_session:
        return

    qq_messages = [
        dict(message)
        for message in (qq_session.get("messages") or [])
        if isinstance(message, dict)
    ]
    web_session["messages"] = qq_messages
    web_session["system_prompt"] = str(qq_session.get("system_prompt") or "")
    web_session["last_message"] = str(qq_session.get("last_message") or "")
    web_session["updated_at"] = datetime.datetime.now().isoformat()
    server.session_store.set_session(web_session_id, web_session)


def _load_session_prompt_text(user_id: str = None, group_id: str = None) -> str:
    base_dir = _get_project_root()
    if user_id:
        prompt_file = os.path.join(
            base_dir, "resources", "prompts", "user", f"user_{user_id}.txt"
        )
    elif group_id:
        prompt_file = os.path.join(
            base_dir, "resources", "prompts", "group", f"group_{group_id}.txt"
        )
    else:
        return ""
    try:
        if os.path.exists(prompt_file):
            with open(prompt_file, encoding="utf-8") as f:
                return f.read().strip()
    except Exception as e:
        print(f"[QQ Character] failed to load session prompt: {e}")
    return ""


def _iter_character_dicts():
    base_dir = _get_project_root()
    profiles = _load_json_file(
        os.path.join(base_dir, "data", "character", "profiles.json"), {}
    )
    if isinstance(profiles, dict):
        for value in profiles.values():
            if isinstance(value, dict):
                yield value

    current = _load_json_file(
        os.path.join(base_dir, "resources", "prompts", "personality.json"), {}
    )
    if isinstance(current, dict):
        yield current

    presets = _load_json_file(
        os.path.join(base_dir, "data", "web", "custom_personality_presets.json"), []
    )
    if isinstance(presets, list):
        for value in presets:
            if isinstance(value, dict):
                yield value


def _resolve_qq_character_name(user_id: str = None, group_id: str = None) -> str:
    session_prompt = _load_session_prompt_text(user_id=user_id, group_id=group_id)
    if session_prompt:
        for character in _iter_character_dicts():
            system_prompt = str(character.get("systemPrompt") or "").strip()
            if system_prompt and system_prompt == session_prompt:
                return str(character.get("name") or character.get("id") or "").strip()

    current = _load_json_file(
        os.path.join(_get_project_root(), "resources", "prompts", "personality.json"),
        {},
    )
    if isinstance(current, dict):
        return str(current.get("name") or current.get("id") or "").strip()
    return ""


def _save_legacy_qq_histories():
    try:
        dump_json("saved_message/user_messages.json", user_messages)
        dump_json("saved_message/group_messages.json", group_messages)
    except Exception as e:
        print(f"保存历史记录失败: {e}")


def _get_qq_store() -> QQSessionStore:
    return QQSessionStore(
        user_messages=user_messages,
        group_messages=group_messages,
        prompt_loader=load_prompt,
        max_history=MAX_HISTORY_LENGTH,
        save_callback=_save_legacy_qq_histories,
    )


def load_canonical_qq_messages(
    user_id: str = None,
    group_id: str = None,
    group_user_id: str = None,
    include_memories: bool = True,
) -> list[dict[str, Any]]:
    session_id = get_qq_session_id(
        user_id=str(user_id) if user_id else None,
        group_id=str(group_id) if group_id else None,
        group_user_id=str(group_user_id) if group_user_id else None,
    )
    if session_id:
        try:
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
            if server:
                session = server.session_store.get_session(session_id)
                if session and isinstance(session.get("messages"), list):
                    normalized = _normalize_canonical_qq_messages(
                        session.get("messages", []),
                        session_mode=str(session.get("session_mode") or ""),
                        inherit_character=ModePolicy.character_features_enabled(session),
                    )
                    if normalized:
                        return normalized
        except Exception:
            pass

    qq_store = _get_qq_store()
    if user_id:
        return qq_store.ensure_history(user_id=str(user_id), include_memories=include_memories)
    if group_id:
        return qq_store.ensure_history(
            group_id=str(group_id),
            group_user_id=str(group_user_id) if group_user_id else None,
            include_memories=include_memories,
        )
    return []


def _normalize_canonical_qq_messages(
    messages: list[dict[str, Any]],
    *,
    session_mode: str = "",
    inherit_character: bool = False,
) -> list[dict[str, Any]]:
    normalized = []
    # 开了「agent 继承角色能力」时保留 system prompt（角色卡/记忆仍在用）
    is_agent = session_mode == "agent" and not inherit_character
    for msg in messages:
        role = str(msg.get("role") or "")
        if role not in ("system", "user", "assistant"):
            continue
        content = msg.get("content", "")
        if is_agent and role == "system":
            content = ""
        normalized.append(
            {
                "role": role,
                "content": content,
                "timestamp": msg.get("timestamp", ""),
            }
        )
    return normalized


# ============================================================================
# QQ 管道回调
# ============================================================================

# QQ 频道角色运行时缓存
_qq_character_runtime = None


class QQCallbacks(PipelineCallbacks):
    """QQ 频道的管道回调实现。"""

    def __init__(
        self,
        qq_store: QQSessionStore,
        user_id: str = None,
        group_id: str = None,
        group_user_id: str = None,
    ):
        self.qq_store = qq_store
        self.user_id = str(user_id) if user_id else None
        self.group_id = str(group_id) if group_id else None
        self.group_user_id = str(group_user_id) if group_user_id else None

    def load_messages(self, ctx: PipelineContext) -> list[dict[str, Any]]:
        """从 QQSessionStore 加载历史消息。"""
        # agent 会话默认不注入角色记忆；开了「继承角色能力」则照常加载
        return load_canonical_qq_messages(
            user_id=self.user_id,
            group_id=self.group_id,
            group_user_id=self.group_user_id,
            include_memories=ctx.mode_policy.inherits_character,
        )

    def auto_compress_context(
        self, ctx: PipelineContext, messages: list, threshold_chars: int
    ) -> list:
        """上下文达到 80% 阈值时自动摘要压缩（失败时按未压缩继续）。"""
        from nbot.core.auto_compression import auto_compress
        from nbot.core.token_stats import PURPOSE_COMPRESSION, get_token_stats_manager

        try:
            from nbot.services.ai import ai_client as _ai_client
        except Exception:
            _ai_client = None
        if not _ai_client or not getattr(_ai_client, "base_url", ""):
            return messages

        live = self.qq_store.ensure_history(
            user_id=self.user_id,
            group_id=self.group_id,
            group_user_id=self.group_user_id,
        )
        if not live:
            return messages

        import time as _time

        ts_key = f"{self.user_id or ''}:{self.group_id or ''}:{self.group_user_id or ''}"
        ts_map = getattr(self.qq_store, "_auto_compress_ts", None)
        if ts_map is None:
            ts_map = {}
            self.qq_store._auto_compress_ts = ts_map
        last_ts = float(ts_map.get(ts_key, 0.0))

        def _on_usage(usage, model):
            if not usage:
                return
            try:
                get_token_stats_manager().record_usage(
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(usage, "total_tokens", 0) or 0,
                    model=model or getattr(_ai_client, "model", "") or "",
                    session_id=ts_key,
                    channel_type="qq",
                    source="qq",
                    purpose=PURPOSE_COMPRESSION,
                )
            except Exception:
                pass

        try:
            new_messages, info = auto_compress(
                live,
                chat_complete=lambda ms: _ai_client.chat_completion(
                    model=None, messages=ms, stream=False
                ),
                max_context_chars=max(threshold_chars, 1),
                trigger_ratio=1.0,
                last_compress_ts=last_ts,
                on_usage=_on_usage,
                model_label=getattr(_ai_client, "model", "") or "",
            )
        except Exception as exc:
            _log.debug("[AutoCompress] QQ 上下文压缩失败: %s", exc)
            return messages

        if not info.get("compressed"):
            return messages

        # 原地替换会话桶内容并持久化
        live[:] = new_messages
        self.qq_store.save()
        ts_map[ts_key] = _time.time()
        _log.info(
            "[AutoCompress] QQ 会话 %s 自动压缩 %d 条消息",
            ts_key, info.get("compressed_count", 0),
        )
        return new_messages

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        include_memories = ctx.mode_policy.inherits_character

        # 检查是否启用新的角色运行时
        # 如果启用，返回空字符串，让角色运行时的 prompt stack 处理
        try:
            from nbot.web.utils.config_loader import get_character_runtime_config
            config = get_character_runtime_config()
            qq_runtime_config = config.get("channels", {}).get("qq", {}).get("character_runtime", {})
            qq_enabled = qq_runtime_config.get("enabled", False)
            qq_legacy_enabled = qq_runtime_config.get("legacy_prompt_enabled", False)

            if qq_enabled and not qq_legacy_enabled:
                # 新角色运行时已启用，旧 prompt 已禁用
                # 返回空字符串，让 Pipeline 使用角色运行时的 prompt
                return ""
        except Exception:
            pass

        # 兼容旧版：加载旧的 prompt
        return load_prompt(
            user_id=self.user_id,
            group_id=self.group_id,
            include_skills=True,
            include_memories=include_memories,
        )

    def search_knowledge(self, ctx: PipelineContext, query: str) -> str:
        text, citations = self.search_knowledge_with_citations(ctx, query)
        return text

    def search_knowledge_with_citations(
        self, ctx: PipelineContext, query: str
    ) -> tuple[str, list[dict[str, Any]]]:
        """知识库检索：命中时把 citations 一并带进上下文元数据。"""
        text, citations = search_knowledge_base_with_citations(
            query, self.user_id, self.group_id
        )
        if citations:
            ctx.metadata["knowledge_citations"] = citations
        return text, citations

    def save_assistant_message(
        self, ctx: PipelineContext, message: dict[str, Any]
    ) -> None:
        """QQ 消息通过 BotAPI 补丁自动保存，Token 统计由 on_response_complete 处理。"""
        self.qq_store.save()

    def get_workspace_context(self, ctx: PipelineContext) -> dict[str, Any]:
        # 从系统获取当前角色名
        character_name = _resolve_qq_character_name(self.user_id, self.group_id)
        return get_workspace_context(self.user_id, self.group_id, self.group_user_id, character_name)

    def get_character_context(self, ctx: PipelineContext):
        """返回 QQ 频道的角色身份标识"""
        from nbot.character.adapters.nekobot import get_qq_character_context

        personality_name = _resolve_qq_character_name(self.user_id, self.group_id)
        return get_qq_character_context(
            user_id=self.user_id or self.group_user_id or "",
            group_id=self.group_id,
            personality_name=personality_name,
        )

    def get_character_runtime(self, ctx: PipelineContext):
        """返回角色运行时实例"""
        global _qq_character_runtime
        if _qq_character_runtime is not None:
            return _qq_character_runtime

        try:
            from nbot.character.adapters.nekobot import get_character_runtime_from_server
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
            if server:
                _qq_character_runtime = get_character_runtime_from_server(server)
                return _qq_character_runtime
        except Exception:
            pass

        # 如果没有 server，直接创建 runtime
        try:
            from nbot.character.memory import PromptManagerMemoryAdapter
            from nbot.character.planner import ReactionPlanner
            from nbot.character.policies import SignalAnalyzer
            from nbot.character.repository import (
                CharacterStateRepository,
                ProfileRepository,
                RelationshipRepository,
            )
            from nbot.character.runtime import CharacterRuntime
            from nbot.character.state_history import StateHistoryStore
            from nbot.character.state_machine import StateMachine
            from nbot.character.storage.world_book_store import WorldBookStore

            base_dir = _get_project_root()
            profile_repo = ProfileRepository(base_dir)

            # 同步 personality.json 到 profiles.json
            from nbot.character.models import CharacterProfile
            personality = _load_json_file(
                os.path.join(base_dir, "resources", "prompts", "personality.json"), {}
            )
            if personality:
                profile = CharacterProfile.from_personality_dict(personality)
                if not profile.id:
                    profile.id = profile.name or "default"
                profile_repo.save(profile)

            _hook_rt = None
            try:
                from nbot.hooks.manager import get_hook_manager
                _hook_rt = get_hook_manager()
            except Exception:
                pass
            _qq_character_runtime = CharacterRuntime(
                profile_repo=profile_repo,
                state_repo=CharacterStateRepository(base_dir),
                relationship_repo=RelationshipRepository(base_dir),
                memory_service=PromptManagerMemoryAdapter(),
                signal_analyzer=SignalAnalyzer(),
                planner=ReactionPlanner(),
                state_machine=StateMachine(),
                world_book_store=WorldBookStore(base_dir),
                hook_runtime=_hook_rt,
                history_store=StateHistoryStore(base_dir),
            )
            return _qq_character_runtime
        except Exception as exc:
            print(f"[QQCharacterRuntime] failed to create runtime: {exc}")
        return None

    def check_confirmation(
        self, ctx: PipelineContext, user_input: str
    ) -> str | None:
        """QQ 确认关键词检测。"""
        if not TOOLS_AVAILABLE or not get_pending_by_session:
            return None
        try:
            session_id = get_qq_session_id(
                self.user_id, self.group_id, self.group_user_id
            )
            session_type = "qq_private" if self.user_id else "qq_group"
            stripped = (user_input or "").strip().lower()
            if not stripped:
                return None
            # 去掉末尾标点，便于匹配"确认。"等
            stripped_clean = stripped.rstrip("。.!！?？~~")
            # 单字/单字符关键词仅精确匹配，避免"是否""boy"等误判
            _confirm_exact = {"是", "y", "ok", "执行"}
            _reject_exact = {"否", "n", "cancel"}
            multi_confirm = _CONFIRM_KEYWORDS - _confirm_exact
            multi_reject = _REJECT_KEYWORDS - _reject_exact

            is_confirm = (
                stripped in _CONFIRM_KEYWORDS
                or stripped_clean in _CONFIRM_KEYWORDS
                or any(stripped_clean == kw for kw in multi_confirm)
                or stripped_clean in _confirm_exact
            )
            is_reject = (
                stripped in _REJECT_KEYWORDS
                or stripped_clean in _REJECT_KEYWORDS
                or any(stripped_clean == kw for kw in multi_reject)
                or stripped_clean in _reject_exact
            )
            if is_confirm and not is_reject:
                request_id = get_pending_by_session(session_id, session_type=session_type)
                if request_id:
                    return "confirm"
            elif is_reject and not is_confirm:
                request_id = get_pending_by_session(session_id, session_type=session_type)
                if request_id:
                    return "reject"
        except Exception:
            pass
        return None

    def on_response_complete(
        self, ctx: PipelineContext, result: PipelineResult
    ) -> None:
        """使用管道返回的真实 token 用量更新统计。"""
        usage = result.usage
        if not usage:
            return
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)
        if not total_tokens:
            return

        from nbot.core.token_stats import get_token_stats_manager

        # 优先使用 API 实际返回的模型名，回退到配置的模型
        meta = result.metadata or {}
        model = meta.get("model_name", "") or meta.get("model_id", "") or _get_active_model_name()
        runtime_ai = refresh_runtime_ai_config()
        session_id = str(self.user_id) if self.user_id else str(self.group_id)
        channel_type = "private" if self.user_id else "group"
        get_token_stats_manager().record_usage(
            prompt_tokens,
            completion_tokens,
            total_tokens=total_tokens,
            model=model,
            session_id=session_id,
            channel_type=channel_type,
            user_id=session_id,
            source="qq",
            input_price=runtime_ai.get("input_price"),
            output_price=runtime_ai.get("output_price"),
        )

    def send_response(
        self, ctx: PipelineContext, message: dict[str, Any]
    ) -> None:
        """QQ 频道通过 BotAPI 补丁自动发送消息，此处为空操作。"""
        pass

    # ---- 表情包 ----

    def send_sticker(
        self, ctx: PipelineContext, sticker_info: dict[str, Any]
    ) -> None:
        """QQ 频道通过 BotAPI 发送表情包图片（单独消息）"""
        try:
            import nbot.commands as _cmd_mod
            bot_instance = getattr(_cmd_mod, "bot", None)
            if not bot_instance or not hasattr(bot_instance, "api"):
                print("[Sticker] QQ Bot 实例不可用，跳过发送")
                return

            image_url = sticker_info.get("url", "")
            if not image_url:
                return

            api = bot_instance.api
            # 根据会话类型选择发送方式：私聊或群聊
            if self.user_id:
                # 私聊
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(api.post_private_msg(self.user_id, image=image_url))
                finally:
                    loop.close()
            elif self.group_id:
                # 群聊
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(api.post_group_msg(self.group_id, image=image_url))
                finally:
                    loop.close()

            print(
                f"[Sticker] QQ 表情包已发送: target={self.user_id or self.group_id} "
                f"url={image_url[:80]}"
            )
        except Exception as e:
            print(f"[Sticker] QQ 表情包发送失败: {e}")

    # ---- 角色生图 ----

    def send_image(
        self, ctx: PipelineContext, image_info: dict[str, Any]
    ) -> None:
        """QQ 频道通过 BotAPI 发送 AI 生成的图片（单独消息）"""
        try:
            import nbot.commands as _cmd_mod
            bot_instance = getattr(_cmd_mod, "bot", None)
            if not bot_instance or not hasattr(bot_instance, "api"):
                _log.warning("[ImageGen] QQ Bot 实例不可用，跳过发送")
                return

            image_url = image_info.get("url", "")
            if not image_url:
                return

            api = bot_instance.api
            if self.user_id:
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(api.post_private_msg(self.user_id, image=image_url))
                finally:
                    loop.close()
            elif self.group_id:
                import asyncio
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(api.post_group_msg(self.group_id, image=image_url))
                finally:
                    loop.close()

            _log.info(
                "[ImageGen] QQ 图片已发送: target=%s trigger=%s url=%s",
                self.user_id or self.group_id,
                image_info.get("trigger", ""),
                image_url[:80],
            )
        except Exception as exc:
            _log.error("[ImageGen] QQ 图片发送失败: %s", exc)


def search_knowledge_base_with_citations(
    query: str,
    user_id: str = None,
    group_id: str = None,
) -> tuple[str, list[dict[str, Any]]]:
    """检索知识库，返回 ``(格式化文本, citations)``。

    citations 字段与 ``KnowledgeManager.retrieve`` 对齐（doc_id/title/source/
    chunk_index/start_char/end_char/snippet/score/rerank_score/tags）。
    空知识库、未命中、检索异常都返回 ``("", [])``，不抛异常。
    """
    if not KNOWLEDGE_AVAILABLE or not query:
        return "", []

    try:
        km = get_knowledge_manager()
        if not km:
            return "", []
        payload = km.search_with_citations(query, base_id=None, top_k=3)
    except Exception as e:
        _log.warning("[知识库] 检索失败: %s", e)
        return "", []

    results = list(payload.get("results") or [])
    citations = list(payload.get("citations") or [])
    if not results or not citations:
        return "", []

    knowledge_text = "【知识库检索结果】"
    seen_titles: set[str] = set()
    kept_citations: list[dict[str, Any]] = []
    # results 与 citations 一一对应（同一 chosen 顺序），按索引配对
    for (doc, similarity, chunk_content), citation in zip(results, citations):
        try:
            if float(similarity or 0.0) < 0.1:
                continue
        except (TypeError, ValueError):
            continue
        title = str(getattr(doc, "title", "") or citation.get("title") or "")
        if title in seen_titles:
            continue
        seen_titles.add(title)
        kept_citations.append(citation)
        body = chunk_content[:300]
        if len(chunk_content) > 300:
            body += "..."
        knowledge_text += f"\n\n📄 {title}\n{body}"

    if not kept_citations:
        return "", []
    _log.info("[知识库] 检索到 %d 条相关内容（含 citations）", len(kept_citations))
    return knowledge_text, kept_citations


def search_knowledge_base(query: str, user_id: str = None, group_id: str = None) -> str:
    """搜索知识库并返回相关内容（兼容旧调用，丢弃 citations）。"""
    text, _ = search_knowledge_base_with_citations(query, user_id, group_id)
    return text


def get_qq_session_id(user_id=None, group_id=None, group_user_id=None) -> str:
    """
    获取 QQ 端会话的统一 session_id
    私聊: "qq_private_{user_id}"
    群聊: "qq_group_{group_id}_{group_user_id}" 或 "qq_group_{group_id}"
    """
    return build_qq_session_id(user_id, group_id, group_user_id)


def get_workspace_context(user_id=None, group_id=None, group_user_id=None, character_name=None) -> dict:
    """获取工作区上下文信息，用于传递给工具调用"""
    session_id = get_qq_session_id(user_id, group_id, group_user_id)
    if not session_id:
        return {}

    session_type = "qq_private" if user_id else "qq_group"

    # 确保工作区已创建
    if WORKSPACE_AVAILABLE:
        workspace_manager.get_or_create(session_id, session_type)

    context = {
        'session_id': session_id,
        'session_type': session_type,
        'target_id': str(user_id or group_id or ''),
    }
    if user_id:
        context['user_id'] = str(user_id)
    if group_id:
        context['group_id'] = str(group_id)
    if group_user_id:
        context['group_user_id'] = str(group_user_id)

    # 添加角色名（如果提供）
    if character_name:
        context['character_name'] = character_name

    return context


def ensure_workspace(user_id=None, group_id=None, group_user_id=None) -> str:
    """确保会话的工作区存在，返回工作区路径"""
    if not WORKSPACE_AVAILABLE:
        return ""
    session_id = get_qq_session_id(user_id, group_id, group_user_id)
    if not session_id:
        return ""
    session_type = "qq_private" if user_id else "qq_group"
    return workspace_manager.get_or_create(session_id, session_type)


def delete_session_workspace(user_id=None, group_id=None, group_user_id=None) -> bool:
    """删除会话对应的工作区"""
    if not WORKSPACE_AVAILABLE:
        return False
    session_id = get_qq_session_id(user_id, group_id, group_user_id)
    if not session_id:
        return False
    return workspace_manager.delete_workspace(session_id)


def remove_brackets_content(text: str) -> str:
    text = re.sub(r'（.*?）', '', text)
    text = re.sub(r'【.*?】', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'\{.*?\}', '', text)
    text = re.sub(r'\「.*?\」', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text.strip()


def load_memories(user_id=None, group_id=None):
    """加载长期和短期记忆（兼容旧接口，使用新模块）

    .. deprecated::
        建议使用角色运行时的记忆系统，此函数仅为兼容保留。
    """
    import warnings
    warnings.warn(
        "load_memories() 已弃用，请使用角色运行时的记忆系统",
        DeprecationWarning,
        stacklevel=2,
    )
    return prompt_manager.load_memories(user_id, group_id)


def load_prompt(user_id=None, group_id=None, include_skills: bool = True, include_memories: bool = True):
    """加载提示词（兼容旧接口，使用新模块 + 技能列表）

    .. deprecated::
        建议使用角色运行时的 PromptStack 系统，此函数仅为兼容保留。
    """
    import warnings
    warnings.warn(
        "load_prompt() 已弃用，请使用角色运行时的 PromptStack 系统",
        DeprecationWarning,
        stacklevel=2,
    )
    user_id = str(user_id) if user_id else None
    group_id = str(group_id) if group_id else None

    prompt = prompt_manager.load_prompt(user_id, group_id, include_memories=include_memories, include_tools=True)

    if include_skills:
        try:
            from nbot.plugins import get_plugin_manager
            pm = get_plugin_manager()
            from nbot.plugins.dispatcher import get_skill_dispatcher
            dispatcher = get_skill_dispatcher(pm)
            skills_prompt = dispatcher.get_available_skills_prompt()
            if skills_prompt:
                if prompt:
                    prompt = prompt + "\n\n" + skills_prompt
                else:
                    prompt = skills_prompt
        except Exception:
            pass

    return prompt


def online_search(content: str) -> str:
    return ai_client.search(content)


def chat_image(iurl: str) -> str:
    print(f"[图片识别] chat_image 收到请求, URL: {iurl}")
    result = ai_client.describe_image(iurl, "请描述这个图片的内容，仅作描述，不要分析内容")
    print(f"[图片识别] chat_image 返回结果: {result[:50] if result else '空'}...")
    return result or "图片识别失败"


def chat_gif(iurl: str) -> str:
    return ai_client.describe_gif_as_video(iurl) or "GIF识别失败"


def chat_video(vurl: str) -> str:
    return ai_client.describe_video(vurl) or "视频识别失败"


def chat_webpage(wurl: str) -> str:
    max_seq_len = 131071
    if not wurl.startswith("http"):
        wurl = "https://" + wurl
    try:
        import requests
        res = requests.get(wurl, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }, timeout=10)
    except:
        return "网页获取失败"

    html = res.text
    if len(html) > max_seq_len:
        html = html[:max_seq_len]

    return ai_client.describe_webpage_html(html) or "网页解析失败"


def chat_json(content: str) -> str:
    return ai_client.analyze_json(content)


def judge_reply(content: str) -> float:
    return ai_client.should_reply(content)


def chat(content: str = "", user_id=None, group_id=None, group_user_id=None,
         image: bool = False, url=None, video=None, attachments: list = None):
    adapter = get_channel_adapter("qq") or QQChannelAdapter()
    atts = list(attachments or [])
    # 兼容旧版调用方式：image/url → 转为 attachment
    if image and url:
        atts.append({"type": "image", "url": url, "source": "qq"})
    if video:
        atts.append({"type": "video", "url": video, "source": "qq"})
    chat_request = adapter.build_chat_request(
        content=content,
        user_id=str(user_id) if user_id else None,
        attachments=atts,
        metadata={
            "group_id": str(group_id) if group_id else None,
            "group_user_id": str(group_user_id) if group_user_id else None,
        },
    )
    return chat_from_request(chat_request, adapter=adapter).final_content


def chat_from_request(
    chat_request: ChatRequest, adapter: QQChannelAdapter = None
) -> ChatResponse:
    agent_service = AgentService()
    register_channel_handler("qq", _run_qq_chat_request)
    adapter = adapter or get_channel_adapter("qq") or QQChannelAdapter()
    return agent_service.process(chat_request, adapter=adapter)


def _run_qq_chat_request(
    chat_request: ChatRequest, adapter: QQChannelAdapter = None
) -> ChatResponse:
    adapter = adapter or get_channel_adapter("qq") or QQChannelAdapter()
    runtime_ai = refresh_runtime_ai_config()
    channel_capabilities = adapter.get_capabilities()

    content = chat_request.content
    user_id = chat_request.user_id
    group_id = chat_request.metadata.get("group_id")
    group_user_id = chat_request.metadata.get("group_user_id")
    session_id = chat_request.conversation_id
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qq_store = _get_qq_store()

    if user_id:
        user_id = str(user_id)
    if group_id:
        group_id = str(group_id)

    # === 命令处理（Gateway 上下文）===
    # 检查是否是命令（以 / 开头）
    if content and content.startswith("/"):
        try:
            from nbot.commands import match_command
            handler, cmd = match_command(content)
            if handler:
                # 创建一个模拟的 msg 对象用于命令处理
                class MockMsg:
                    def __init__(self, user_id, group_id, raw_message, conversation_id):
                        self.user_id = user_id
                        self.group_id = group_id
                        self.raw_message = raw_message
                        self.conversation_id = conversation_id
                        self._reply_text = None

                    async def reply(self, text=None, **kwargs):
                        self._reply_text = text

                is_group = bool(group_id)
                mock_msg = MockMsg(
                    user_id=group_user_id or user_id,
                    group_id=group_id,
                    raw_message=content,
                    conversation_id=session_id,
                )

                # 执行命令处理函数
                import asyncio
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # 如果事件循环已在运行，使用 ensure_future
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            future = pool.submit(asyncio.run, handler(mock_msg, is_group))
                            future.result(timeout=30)
                    else:
                        loop.run_until_complete(handler(mock_msg, is_group))
                except Exception as e:
                    _log.warning(f"[QQ] 命令执行异常 cmd={cmd} error={e}")

                # 返回命令响应
                if mock_msg._reply_text:
                    return ChatResponse(final_content=mock_msg._reply_text)
        except ImportError:
            _log.debug("[QQ] 无法导入命令模块，跳过命令处理")
        except Exception as e:
            _log.warning(f"[QQ] 命令处理异常 error={e}")

    # === Gateway 事件记录：开始（QQ 频道）===
    trace_id = ""
    try:
        from nbot.gateway.gateway import get_gateway as _get_gw
        from nbot.gateway.trace import TraceFactory
        _gw = _get_gw()
        if _gw and _gw.event_store:
            _tf = getattr(_gw, 'trace_factory', None) or TraceFactory()
            trace_id = _tf.new_trace_id()
            _gw.record_lifecycle_event(
                trace_id=trace_id, channel_id="qq", status="received",
                event_type="message", conversation_id=session_id,
                user_id=str(group_user_id or user_id),
                raw_event={"content": content[:150], "sender": group_user_id or user_id},
                metadata={"content_length": len(content), "group_id": group_id or ""},
            )
            _gw.record_lifecycle_event(
                trace_id=trace_id, channel_id="qq", status="dispatched",
                conversation_id=session_id,
            )
    except Exception:
        trace_id = ""

    # === 确认/拒绝待执行命令检测 ===
    if content and TOOLS_AVAILABLE:
        session_id_check = get_qq_session_id(user_id, group_id, group_user_id)
        _session_type_check = "qq_private" if user_id else "qq_group"
        content = handle_tool_confirmation(
            content, session_id_check,
            log_prefix="QQ Confirm",
            session_type=_session_type_check,
        )
        chat_request.content = content

    # === 时间前缀 + 用户信息 ===
    pre_text = f"用户{group_user_id}说：" if group_user_id else ""
    # 图片/视频描述已由 MessagePreprocessor 中间件注入 content
    enhanced_content = f"(当前时间：{now_time})\n{pre_text}{content}"

    # URL 链接描述
    pattern = r"(?:https?:\/\/)?(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})+(?:\/[^\s?]*)?(?:\?[^\s]*)?"
    matches = re.findall(pattern, content)
    if matches:
        des = ""
        for i, match in enumerate(matches, 1):
            des += f"第{i}个链接{match}的描述：" + chat_webpage(match) + "\n"
        enhanced_content += f"\n{pre_text}{des}"

    # 更新 chat_request 内容为预处理后的内容
    chat_request.content = enhanced_content

    # 记录用户消息
    record_user_message(content, user_id, group_id, group_user_id)

    # === 确定是否启用工具 ===
    tools = None
    if (
        TOOLS_AVAILABLE
        and runtime_ai.get("supports_tools", True)
        and channel_capabilities.supports_file_send
    ):
        tools = TOOL_DEFINITIONS

    # === 通过管道处理 AI 响应 ===
    ctx = PipelineContext(chat_request=chat_request, adapter=adapter)
    ctx.metadata["channel_type"] = "private" if user_id else "group"
    ctx.metadata["source"] = "qq"

    # 传递 session_mode 供管道判断是否跳过角色运行时 / 自动记忆
    try:
        from nbot.web.server import WebChatServer as _WCS

        _wcs = _WCS.get_instance()
        if _wcs:
            _sid = get_qq_session_id(user_id, group_id, group_user_id)
            _sess = _wcs.session_store.get_session(_sid) if _sid else {}
            if (_sess or {}).get("session_mode"):
                ctx.metadata["session_mode"] = _sess["session_mode"]
            # agent 继承角色能力（P2-8）：会话级开关，默认关闭
            if (_sess or {}).get("agent_inherit_character") is not None:
                ctx.metadata["agent_inherit_character"] = _sess["agent_inherit_character"]
            # 注入会话级 auto_state 触发轮次（None 表示用全局默认）
            if (_sess or {}).get("auto_state_interval") is not None:
                ctx.metadata["auto_state_interval"] = _sess["auto_state_interval"]
            # Agent 持久目标 / 规格（/goal、/spec 命令设置）
            if (_sess or {}).get("agent_goal"):
                ctx.metadata["agent_goal"] = _sess["agent_goal"]
            if (_sess or {}).get("agent_spec"):
                ctx.metadata["agent_spec"] = _sess["agent_spec"]
            # 用户人设（persona）：与 Web 端一致，命中则注入 system prompt 末尾
            from nbot.core.agent_service import apply_user_persona_metadata

            apply_user_persona_metadata(chat_request, ctx.metadata, _sess or None)
    except Exception:
        pass

    callbacks = QQCallbacks(qq_store, user_id, group_id, group_user_id)

    pipeline = AIPipeline()
    hook_runtime = None
    try:
        from nbot.hooks.manager import get_hook_manager
        hook_runtime = get_hook_manager()
    except Exception:
        pass

    # === 群聊模式检测 ===
    group_context = None
    if group_id:
        try:
            from nbot.group.manager import get_group_manager
            gm = get_group_manager()
            channel_id = f"qq:group:{group_id}"
            group = gm.get_group_by_channel(channel_id)
            if group and group.character_ids:
                # Load character profiles
                profiles = {}
                for cid in group.character_ids:
                    p = _load_json_file(
                        os.path.join(_get_project_root(), "data", "character", "profiles.json"), {}
                    )
                    if isinstance(p, dict) and cid in p:
                        profiles[cid] = p[cid]
                # Build group context
                from nbot.group.narrator import NarratorCharacter
                from nbot.group.scheduler import SpeakerScheduler
                group_context = {
                    "group": group,
                    "character_profiles": profiles,
                    "scheduler": SpeakerScheduler.instance(),
                    "narrator": NarratorCharacter.instance(),
                    "auto_narrate": group.config.auto_narrate,
                    "recent_messages": [],
                }
        except Exception as e:
            _log.debug("group context build failed: %s", e)

    result = pipeline.process(ctx, callbacks, tools=tools, max_context_chars=100000, hook_runtime=hook_runtime, group_context=group_context)

    # === 群聊 @mention 跨角色对话 ===
    global _cross_talk_queue
    _cross_talk_queue = []
    if group_context and group_context.get("group") and not ctx.metadata.get("cross_talk_triggered"):
        _group = group_context["group"]
        if _group.config.allow_character_cross_talk:
            try:
                from nbot.group.cross_talk import collect_mentions_from_round, process_cross_talk
                _speaker_name = ctx.metadata.get("group_speaker_name", "")
                _round_msgs = [{
                    "role": "assistant",
                    "content": result.final_content or "",
                    "sender": _speaker_name,
                }]
                _mentions = collect_mentions_from_round(
                    _round_msgs,
                    _group.character_ids,
                    group_context.get("character_profiles", {}),
                )
                if _mentions:
                    _max_mentions = getattr(_group.config, "cross_talk_max_mentions", 5)

                    def _build_qq_cross_talk_ctx(speaker_id=""):
                        cross_ctx = PipelineContext(
                            chat_request=chat_request,
                            adapter=adapter,
                            metadata=dict(ctx.metadata),
                        )
                        cross_ctx.metadata["group_speaker"] = speaker_id
                        cross_ctx.metadata["group_speaker_name"] = (
                            group_context.get("character_profiles", {}).get(speaker_id, {}).get("name", speaker_id)
                        )
                        cross_ctx.metadata["cross_talk_triggered"] = True
                        return cross_ctx

                    def _enqueue_cross_talk_msg(msg_dict):
                        name = msg_dict.get("sender", "")
                        content = msg_dict.get("content", "")
                        if name and content:
                            display = clean_response_content(content)
                            display = extract_display_text(display)
                            if display:
                                _cross_talk_queue.append({"speaker_name": name, "content": display})

                    process_cross_talk(
                        _mentions, _max_mentions,
                        pipeline=pipeline,
                        callbacks=callbacks,
                        group_context=group_context,
                        base_metadata=dict(ctx.metadata),
                        chat_request=chat_request,
                        adapter=adapter,
                        tools=tools,
                        max_context_chars=100000,
                        hook_runtime=hook_runtime,
                        build_cross_talk_context=_build_qq_cross_talk_ctx,
                        send_cross_talk_message=_enqueue_cross_talk_msg,
                    )
            except Exception as ct_err:
                _log.error("QQ cross-talk failed: %s", ct_err)

    # === Gateway 事件记录：完成（QQ 频道）===
    if trace_id:
        try:
            from nbot.gateway.gateway import get_gateway as _get_gw2
            _gw2 = _get_gw2()
            if _gw2 and _gw2.event_store:
                reply_preview = (result.final_content or "")[:200]
                _gw2.record_lifecycle_event(
                    trace_id=trace_id, channel_id="qq", status="delivered",
                    conversation_id=session_id,
                    raw_event={"reply_preview": reply_preview} if reply_preview else None,
                    metadata={"reply_length": len(reply_preview)} if reply_preview else None,
                )
        except Exception:
            pass

    # === 后处理 ===
    assistant_response = clean_response_content(result.final_content)
    display_response = extract_display_text(assistant_response)

    # 群聊模式：添加角色名前缀
    if group_context and group_context.get("group"):
        group = group_context["group"]
        speaker = ctx.metadata.get("group_speaker", "")
        speaker_name = ctx.metadata.get("group_speaker_name", speaker)
        if speaker_name and display_response:
            display_response = f"[{speaker_name}] {display_response}"
    if assistant_response and assistant_response.strip().startswith("{"):
        try:
            fixed = assistant_response.replace(chr(8220), '"').replace(chr(8221), '"').replace(chr(65306), ":")
            parsed = json.loads(fixed)
            if isinstance(parsed, dict) and "msg" in parsed:
                display_response = parsed["msg"]
        except Exception:
            pass

    qq_store.save()

    chat_response = ChatResponse(final_content=display_response)
    chat_response.assistant_message = adapter.build_assistant_message(
        chat_response,
        conversation_id=chat_request.conversation_id,
        sender="AI",
    )
    return chat_response


def _get_active_model_name() -> str:
    """获取当前活跃的模型名称。"""
    runtime_ai = refresh_runtime_ai_config()
    return runtime_ai.get("model", "") or ""


def _sync_to_web_session(role, content, user_id=None, group_id=None, group_user_id=None):
    """将消息同步到 Web 会话 - 通过 sync_qq_messages 统一管理"""
    from nbot.web.server import WebChatServer

    server = WebChatServer.get_instance()
    if not server:
        return

    qq_session_id = server.sync_qq_messages(
        user_id=str(user_id) if user_id else None,
        group_id=str(group_id) if group_id else None,
        group_user_id=str(group_user_id) if group_user_id else None,
        create_if_not_exists=True,
    )
    try:
        _sync_bound_web_session(server, qq_session_id)
    except Exception as e:
        print(f"[QQ Binding] failed to sync bound web session: {e}")


def _record_message(role, content, user_id=None, group_id=None, group_user_id=None):
    """记录消息到内存和文件（兼容旧接口，同时使用新模块）"""
    if not content:
        return

    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if role == "user" and "(当前时间：" not in content:
        record_content = f"(当前时间：{now_time})\n{content}"
    elif role == "assistant":
        # 解析 JSON 内容，提取 msg
        display_content = content
        if content and content.strip().startswith('{'):
            try:
                # 替换中文引号和冒号为英文
                # 8220=" 8221=" 65306=:
                fixed_content = content.replace(chr(8220), '"').replace(chr(8221), '"').replace(chr(65306), ':')
                parsed = json.loads(fixed_content)
                if isinstance(parsed, dict) and 'msg' in parsed:
                    display_content = parsed['msg']
            except Exception as e:
                print(f"[DEBUG] JSON parse failed: {e}, content: {content[:100]}")
        record_content = display_content
    else:
        record_content = content

    qq_store = _get_qq_store()
    qq_adapter = get_channel_adapter("qq") or QQChannelAdapter()

    def _sync_manager_message(target_id, **payload_kwargs):
        base_message = {
            "role": payload_kwargs.get("role"),
            "content": payload_kwargs.get("content"),
        }
        manager_message = qq_adapter.build_manager_payload_from_message(
            base_message,
            default_role=payload_kwargs.get("role"),
            default_content=payload_kwargs.get("content"),
            user_id=payload_kwargs.get("user_id", ""),
            group_id=payload_kwargs.get("group_id", ""),
            group_user_id=payload_kwargs.get("group_user_id", ""),
        )
        if payload_kwargs.get("user_id"):
            message_manager.add_qq_private_message(
                target_id, create_message(**manager_message)
            )
        else:
            message_manager.add_qq_group_message(
                target_id, create_message(**manager_message)
            )

    if user_id:
        user_id = str(user_id)
        qq_store.append_message(role=role, content=record_content, user_id=user_id)
        
        # 同时记录到新消息模块
        _sync_manager_message(
            user_id,
            role=role,
            content=record_content,
            user_id=user_id,
        )
        _sync_to_web_session(role, record_content, user_id=user_id)
    
    elif group_id:
        group_id = str(group_id)
        qq_store.append_message(
            role=role,
            content=record_content,
            group_id=group_id,
            group_user_id=group_user_id,
        )
        
        # 同时记录到新消息模块（使用 group_id 作为文件标识）
        # 只有用户消息才设置 sender，AI 回复 sender 为空
        _sync_manager_message(
            group_id,
            role=role,
            content=record_content,
            group_id=group_id,
            group_user_id=group_user_id,
        )
        _sync_to_web_session(
            role,
            record_content,
            group_id=group_id,
            group_user_id=group_user_id,
        )


def log_to_group_full_file(group_id, user_id, nickname, content, timestamp=None):
    if not group_id or not content:
        return

    group_id = str(group_id)
    user_id = str(user_id)
    content = str(content).strip()

    now_ts = time.time()
    last_entry = last_log_entry.get(group_id)
    if last_entry and last_entry['user_id'] == user_id and last_entry['content'] == content:
        if now_ts - last_entry['time'] < 1.0:
            return

    last_log_entry[group_id] = {
        'user_id': user_id,
        'content': content,
        'time': now_ts
    }

    if timestamp:
        now = timestamp
    else:
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    group_id = str(group_id)
    user_id = str(user_id)
    line = f"[{now}] [{group_id}] [{user_id}] {nickname}: {content}\n"
    base_dir = os.path.join("saved_message", "group_full")
    os.makedirs(base_dir, exist_ok=True)
    file_path = os.path.join(base_dir, f"group_{group_id}_{date_str}.txt")
    try:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception as e:
        print(f"写入群聊日志失败: {e}")


def record_assistant_message(content, user_id=None, group_id=None, group_user_id=None):
    _record_message("assistant", content, user_id, group_id, group_user_id)


def record_user_message(content, user_id=None, group_id=None, group_user_id=None):
    _record_message("user", content, user_id, group_id, group_user_id)


def summarize_group_text(text: str) -> str:
    text = text.strip()
    if not text:
        return "没有可总结的聊天记录喵~"
    system_prompt = "你是一个群聊记录总结助手，只根据提供的内容生成简洁的中文摘要。"
    user_prompt = (
        "下面是一整个QQ群的一段聊天记录，每一行代表一条消息，包含时间、群号、QQ号或昵称以及内容。\n"
        "请用中文总结出群聊的大致内容和几个主要话题，可以适当分点列出，不要复述所有细节：\n"
        f"{text}"
    )
    try:
        runtime_ai = refresh_runtime_ai_config()
        summary = ai_client.summarize_text(
            system_prompt,
            user_prompt,
            model=runtime_ai.get("model") or ai_client.model,
        )
        return summary or "总结结果为空喵~"
    except Exception:
        return "总结时出错喵，请稍后再试~"


def generate_today_summary(user_id=None, group_id=None) -> str:
    runtime_ai = refresh_runtime_ai_config()
    today_str = datetime.datetime.now().strftime("%Y-%m-%d")
    if group_id:
        group_id_str = str(group_id)
        base_dir = os.path.join("saved_message", "group_full")
        file_path = os.path.join(base_dir, f"group_{group_id_str}_{today_str}.txt")
        if not os.path.exists(file_path):
            return "今天群里还没有记录到消息喵~"
        try:
            with open(file_path, encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            return "读取群聊记录失败喵~"
        if not text:
            return "今天群里还没有记录到消息喵~"
        return summarize_group_text(text)
    if user_id:
        messages_list = load_canonical_qq_messages(user_id=str(user_id))
        if not messages_list:
            return "今天还没有和我聊天喵~"
        lines = []
        today_messages = 0
        for m in messages_list:
            content = m.get("content", "")
            role = m.get("role", "")
            timestamp = str(m.get("timestamp", "") or "")
            if not timestamp.startswith(today_str):
                continue
            if role in ("user", "assistant"):
                today_messages += 1
                lines.append(f"[{role}] {content}")
        if not today_messages:
            return "今天还没有和我聊天喵~"
        text = "\n".join(lines)
        system_prompt = "你是一个聊天记录总结助手，只根据提供的内容生成简洁的中文摘要。"
        user_prompt = (
            "下面是用户和机器人的历史聊天记录，每条内容中可能包含形如(当前时间：YYYY-MM-DD HH:MM:SS)的时间信息。\n"
            f"请只总结日期为 {today_str} 的对话内容，忽略其他日期的内容。\n"
            "用中文输出一个大约200字的摘要，可以适当分点列出要点，不要重复原句：\n"
            f"{text}"
        )
        try:
            summary = ai_client.summarize_text(
                system_prompt,
                user_prompt,
                model=runtime_ai.get("model") or ai_client.model,
            )
            return summary or "总结结果为空喵~"
        except Exception:
            return "总结时出错喵，请稍后再试~"
    return "没有可总结的聊天记录喵~"
