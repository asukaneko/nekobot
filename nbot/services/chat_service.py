import os
import json
import datetime
import time
import re
import copy
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from nbot.services.ai import (
    ai_client, user_messages, group_messages, MAX_HISTORY_LENGTH,
    refresh_runtime_ai_config,
)
from nbot.core import (
    AgentService,
    build_chat_completion_payload,
    ChatRequest,
    ChatResponse,
    clean_response_content,
    extract_display_text,
    normalize_chat_completion_data,
    prepare_chat_context,
    prompt_manager,
    message_manager,
    QQSessionStore,
    resolve_chat_completion_url,
    resolve_loop_final_content,
    ToolLoopSession,
    ToolLoopExit,
    build_qq_session_id,
    dump_json,
    run_tool_loop_session,
)
from nbot.channels.qq import QQChannelAdapter
from nbot.channels.registry import get_channel_adapter, register_channel_handler
from nbot.core.ai_pipeline import (
    AIPipeline,
    PipelineContext,
    PipelineCallbacks,
    PipelineResult,
    handle_tool_confirmation,
)
from nbot.core.message import create_message

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
        TOOL_DEFINITIONS, execute_tool,
        get_pending_by_session, execute_pending_command, reject_pending_command,
        _CONFIRM_KEYWORDS, _REJECT_KEYWORDS,
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


# ============================================================================
# QQ 管道回调
# ============================================================================


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

    def load_messages(self, ctx: PipelineContext) -> List[Dict[str, Any]]:
        """从 QQSessionStore 加载历史消息。"""
        if self.user_id:
            return self.qq_store.ensure_history(user_id=self.user_id)
        elif self.group_id:
            return self.qq_store.ensure_history(
                group_id=self.group_id, group_user_id=self.group_user_id
            )
        return []

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        return load_prompt(
            user_id=self.user_id,
            group_id=self.group_id,
            include_skills=True,
        )

    def search_knowledge(self, ctx: PipelineContext, query: str) -> str:
        return search_knowledge_base(query, self.user_id, self.group_id)

    def save_assistant_message(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        """QQ 消息通过 BotAPI 补丁自动保存，Token 统计由 on_response_complete 处理。"""
        self.qq_store.save()

    def get_workspace_context(self, ctx: PipelineContext) -> Dict[str, Any]:
        return get_workspace_context(self.user_id, self.group_id, self.group_user_id)

    def check_confirmation(
        self, ctx: PipelineContext, user_input: str
    ) -> Optional[str]:
        """QQ 确认关键词检测。"""
        if not TOOLS_AVAILABLE or not get_pending_by_session:
            return None
        try:
            session_id = get_qq_session_id(
                self.user_id, self.group_id, self.group_user_id
            )
            stripped = (user_input or "").strip().lower()
            is_confirm = any(
                kw == stripped or (len(stripped) <= 4 and kw in stripped)
                for kw in _CONFIRM_KEYWORDS
            )
            is_reject = any(
                kw == stripped or (len(stripped) <= 4 and kw in stripped)
                for kw in _REJECT_KEYWORDS
            )
            if is_confirm and not is_reject:
                request_id = get_pending_by_session(session_id)
                if request_id:
                    return "confirm"
            elif is_reject:
                request_id = get_pending_by_session(session_id)
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
        if not prompt_tokens and not completion_tokens:
            return

        from nbot.core.token_stats import get_token_stats_manager

        model = _get_active_model_name()
        session_id = str(self.user_id) if self.user_id else str(self.group_id)
        channel_type = "private" if self.user_id else "group"
        get_token_stats_manager().record_usage(
            prompt_tokens,
            completion_tokens,
            model=model,
            session_id=session_id,
            channel_type=channel_type,
            user_id=session_id,
        )

    def send_response(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        """QQ 频道通过 BotAPI 补丁自动发送消息，此处为空操作。"""
        pass


def search_knowledge_base(query: str, user_id: str = None, group_id: str = None) -> str:
    """
    搜索知识库并返回相关内容
    
    Args:
        query: 用户查询内容
        user_id: 用户ID
        group_id: 群组ID
        
    Returns:
        知识库相关内容，如果无匹配则返回空字符串
    """
    if not KNOWLEDGE_AVAILABLE or not query:
        return ""
    
    try:
        km = get_knowledge_manager()
        if not km:
            return ""
        
        owner_id = user_id or group_id
        owner_type = "user" if user_id else "group"
        
        results = km.search(query, base_id=None, top_k=3)
        
        if not results:
            return ""
        
        knowledge_text = "【知识库检索结果】\n"
        seen_titles = set()
        
        for doc, similarity, chunk_content in results:
            if similarity < 0.1:
                continue
            if doc.title in seen_titles:
                continue
            seen_titles.add(doc.title)
            
            knowledge_text += f"\n📄 {doc.title}\n"
            knowledge_text += f"{chunk_content[:300]}"
            if len(chunk_content) > 300:
                knowledge_text += "..."
            knowledge_text += "\n"
        
        if seen_titles:
            print(f"[知识库] 检索到 {len(seen_titles)} 条相关内容")
            return knowledge_text
        return ""
        
    except Exception as e:
        print(f"[知识库] 检索失败: {e}")
        return ""


def get_qq_session_id(user_id=None, group_id=None, group_user_id=None) -> str:
    """
    获取 QQ 端会话的统一 session_id
    私聊: "qq_private_{user_id}"
    群聊: "qq_group_{group_id}_{group_user_id}" 或 "qq_group_{group_id}"
    """
    return build_qq_session_id(user_id, group_id, group_user_id)


def get_workspace_context(user_id=None, group_id=None, group_user_id=None) -> dict:
    """获取工作区上下文信息，用于传递给工具调用"""
    session_id = get_qq_session_id(user_id, group_id, group_user_id)
    if not session_id:
        return {}

    session_type = "qq_private" if user_id else "qq_group"

    # 确保工作区已创建
    if WORKSPACE_AVAILABLE:
        workspace_manager.get_or_create(session_id, session_type)

    return {
        'session_id': session_id,
        'session_type': session_type
    }


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
    """加载长期和短期记忆（兼容旧接口，使用新模块）"""
    return prompt_manager.load_memories(user_id, group_id)


def load_prompt(user_id=None, group_id=None, include_skills: bool = True):
    """加载提示词（兼容旧接口，使用新模块 + 技能列表）"""
    user_id = str(user_id) if user_id else None
    group_id = str(group_id) if group_id else None
    
    prompt = prompt_manager.load_prompt(user_id, group_id, include_memories=True, include_tools=True)
    
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
    return result


def chat_gif(iurl: str) -> str:
    return ai_client.describe_gif_as_video(iurl)


def chat_video(vurl: str) -> str:
    return ai_client.describe_video(vurl)


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
        return "链接失效"

    html = res.text
    if len(html) > max_seq_len:
        html = html[:max_seq_len]

    return ai_client.describe_webpage_html(html)


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
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qq_store = _get_qq_store()

    if user_id:
        user_id = str(user_id)
    if group_id:
        group_id = str(group_id)

    # === 确认/拒绝待执行命令检测 ===
    if content and TOOLS_AVAILABLE:
        session_id_check = get_qq_session_id(user_id, group_id, group_user_id)
        content = handle_tool_confirmation(
            content, session_id_check, log_prefix="QQ Confirm"
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
    callbacks = QQCallbacks(qq_store, user_id, group_id, group_user_id)

    pipeline = AIPipeline()
    result = pipeline.process(ctx, callbacks, tools=tools, max_context_chars=100000)

    # === 后处理 ===
    assistant_response = clean_response_content(result.final_content)
    display_response = extract_display_text(assistant_response)
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
    """将消息同步到 Web 会话 - 支持群聊用户独立会话"""
    import os
    import json
    from datetime import datetime
    
    if not user_id and not group_id:
        return
    
    # 确定会话标识和类型
    if user_id:
        # 私聊
        qq_id = str(user_id)
        session_type = 'qq_private'
        session_name = f"私聊 {qq_id}"
        prompt_user_id = user_id
        prompt_group_id = None
    elif group_id and group_user_id:
        # 群聊中特定用户 - 创建独立会话
        qq_id = f"{group_id}_{group_user_id}"
        session_type = 'qq_group_user'
        session_name = f"群{group_id}用户{group_user_id}"
        prompt_user_id = None
        prompt_group_id = group_id
    else:
        # 群聊（兼容旧逻辑，整个群一个会话）
        qq_id = str(group_id)
        session_type = 'qq_group'
        session_name = f"群 {qq_id}"
        prompt_user_id = None
        prompt_group_id = group_id
    
    # 使用相对路径
    data_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'web')
    os.makedirs(data_dir, exist_ok=True)
    from nbot.web.sessions_db import load_sessions as load_sessions_from_db
    from nbot.web.sessions_db import save_sessions as save_sessions_to_db
    
    # 加载现有会话
    sessions = load_sessions_from_db(data_dir)
    
    # 查找会话：检查 name 是否匹配 session_name
    session_id = None
    for sid, session in sessions.items():
        if session.get('name') == session_name:
            session_id = sid
            break
    
    # 如果没找到，创建新会话
    if not session_id:
        import uuid
        session_id = str(uuid.uuid4())
        # 获取提示词
        prompt = load_prompt(user_id=prompt_user_id, group_id=prompt_group_id, include_skills=False)
        sessions[session_id] = {
            'id': session_id,
            'name': session_name,
            'type': session_type,
            'qq_id': qq_id,
            'created_at': datetime.now().isoformat(),
            'messages': [{"role": "system", "content": prompt}] if prompt else [],
            'system_prompt': prompt or ''
        }
    
    # 解析 JSON 内容，提取 msg
    display_content = content
    if content and content.strip().startswith('{'):
        try:
            # 尝试解析 JSON
            parsed = json.loads(content)
            if isinstance(parsed, dict) and 'msg' in parsed:
                display_content = parsed['msg']
        except:
            # 如果解析失败，尝试替换中文引号再解析
            try:
                fixed_content = content.replace('"', '"').replace('"', '"')
                parsed = json.loads(fixed_content)
                if isinstance(parsed, dict) and 'msg' in parsed:
                    display_content = parsed['msg']
            except:
                pass
    
    # 添加消息
    import uuid
    message = {
        'id': str(uuid.uuid4()),
        'role': role,
        'content': display_content,
        'timestamp': datetime.now().isoformat(),
        'sender': 'User' if role == 'user' else 'Bot',
        'source': 'qq'
    }
    sessions[session_id]['messages'].append(message)
    sessions[session_id]['last_message'] = display_content[:100]
    
    # 保存会话
    try:
        save_sessions_to_db(data_dir, sessions)
        print(f"[DEBUG] 已同步消息到 sessions.json, session_id: {session_id}, qq_id: {qq_id}")
    except Exception as e:
        print(f"同步到 Web 会话失败: {e}")


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
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read().strip()
        except Exception:
            return "读取群聊记录失败喵~"
        if not text:
            return "今天群里还没有记录到消息喵~"
        return summarize_group_text(text)
    if user_id:
        key = str(user_id)
        messages_list = user_messages.get(key, [])
        if not messages_list:
            return "今天还没有和我聊天喵~"
        lines = []
        has_today = False
        for m in messages_list:
            content = m.get("content", "")
            role = m.get("role", "")
            if today_str in content:
                has_today = True
            if role in ("user", "assistant"):
                lines.append(f"[{role}] {content}")
        if not has_today:
            return "今天还没有和我聊天喵~"
        text = "\n".join(lines)
        client = None
        try:
            from openai import OpenAI
            client = OpenAI(
                api_key=runtime_ai.get("api_key") or "",
                base_url=runtime_ai.get("base_url") or "",
            )
        except ImportError:
            pass

        if client:
            system_prompt = "你是一个聊天记录总结助手，只根据提供的内容生成简洁的中文摘要。"
            user_prompt = (
                "下面是用户和机器人的历史聊天记录，每条内容中可能包含形如(当前时间：YYYY-MM-DD HH:MM:SS)的时间信息。\n"
                f"请只总结日期为 {today_str} 的对话内容，忽略其他日期的内容。\n"
                "用中文输出一个大约200字的摘要，可以适当分点列出要点，不要重复原句：\n"
                f"{text}"
            )
            try:
                response = client.chat.completions.create(
                    model=runtime_ai.get("model") or ai_client.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    stream=False
                )
                summary = response.choices[0].message.content
                return summary or "总结结果为空喵~"
            except Exception:
                return "总结时出错喵，请稍后再试~"
        return "总结功能不可用喵~"
    return "没有可总结的聊天记录喵~"
