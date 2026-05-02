import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List
from nbot.channels.registry import get_channel_adapter
from nbot.channels.web import WebChannelAdapter
from nbot.core import (
    build_continue_chat_response,
    build_chat_completion_payload,
    ChatRequest,
    ChatResponse,
    extract_tool_call_history,
    normalize_chat_completion_data,
    prepare_chat_context,
    resolve_chat_completion_url,
    ToolLoopExit,
    ToolLoopHooks,
    ToolLoopSession,
    WebSessionStore,
    run_tool_loop_session,
)
from nbot.web.utils.config_loader import get_vision_model_config

try:
    from nbot.core.knowledge import get_knowledge_manager

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    KNOWLEDGE_MANAGER_AVAILABLE = False

# WORKSPACE_AVAILABLE 会在函数中从 server 对象获取

_log = logging.getLogger(__name__)


def _feature_enabled(server, name: str, default: bool = True) -> bool:
    settings = getattr(server, "settings", {}) or {}
    features = settings.get("features")
    if isinstance(features, dict) and name in features:
        return bool(features.get(name))
    return bool(settings.get(name, default))


def _build_channel_assistant_message(
    chat_response: ChatResponse,
    *,
    session_id: str,
    adapter=None,
    sender: str = "AI",
):
    channel_adapter = adapter or get_channel_adapter("web") or WebChannelAdapter()
    return channel_adapter.build_assistant_message(
        chat_response,
        conversation_id=session_id,
        sender=sender,
    )


def _build_change_card(
    *,
    session_id: str,
    parent_message_id: str,
    file_changes,
):
    normalized_changes = []
    summary = {"total": 0, "created": 0, "modified": 0, "deleted": 0}
    change_by_path = {}

    for change in file_changes or []:
        if not isinstance(change, dict):
            continue
        action = str(change.get("action") or "modified")
        if action not in summary:
            action = "modified"
        normalized_change = {
            "action": action,
            "path": change.get("path") or change.get("filename") or "",
            "scope": change.get("scope") or "private",
            "before_preview": change.get("before_preview"),
            "after_preview": change.get("after_preview"),
            "diff_preview": change.get("diff_preview"),
        }
        change_key = normalized_change["path"] or f"unnamed_{len(change_by_path)}"
        change_by_path[change_key] = normalized_change

    normalized_changes = list(change_by_path.values())
    for change in normalized_changes:
        action = change["action"]
        summary[action] += 1
        summary["total"] += 1

    if not normalized_changes:
        return None

    return {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "parent_message_id": parent_message_id,
        "role": "system",
        "type": "change_card",
        "content": "本轮文件变更",
        "summary": summary,
        "file_changes": normalized_changes,
        "timestamp": datetime.now().isoformat(),
        "is_complete": True,
    }


def _emit_change_card(
    server,
    session_store,
    *,
    session_id: str,
    parent_message_id: str,
    file_changes,
):
    if not parent_message_id:
        return None

    change_card = _build_change_card(
        session_id=session_id,
        parent_message_id=parent_message_id,
        file_changes=file_changes,
    )
    if not change_card:
        return None

    session = session_store.get_session(session_id)
    if not session:
        return None

    for msg in session.get("messages", []):
        if (
            msg.get("id") == parent_message_id
            or msg.get("tempId") == parent_message_id
            or msg.get("originalTempId") == parent_message_id
        ):
            if "change_cards" not in msg:
                msg["change_cards"] = []
            msg["change_cards"] = [
                card
                for card in msg["change_cards"]
                if card.get("type") != "change_card"
            ]
            msg["change_cards"].append(change_card)
            break

    if session_store.save_callback:
        session_store.save_callback()
    server.socketio.emit("new_message", change_card, room=session_id)
    return change_card


def trigger_ai_response(
    server,
    session_id: str,
    user_content: str,
    sender: str,
    attachments=None,
    parent_message_id=None,
):
    """Trigger an AI response for a web session."""
    adapter = getattr(server, "web_channel_adapter", None) or get_channel_adapter("web") or WebChannelAdapter()
    chat_request = adapter.build_chat_request(
        conversation_id=session_id,
        content=user_content,
        sender=sender,
        attachments=attachments,
        parent_message_id=parent_message_id,
    )
    return trigger_ai_response_for_request(server, chat_request, adapter=adapter)


def trigger_ai_response_for_request(server, chat_request: ChatRequest, adapter=None):
    adapter = adapter or getattr(server, "web_channel_adapter", None) or get_channel_adapter("web") or WebChannelAdapter()
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )
    session_id = chat_request.conversation_id
    user_content = chat_request.content
    sender = chat_request.sender
    attachments = list(chat_request.attachments or [])
    parent_message_id = chat_request.parent_message_id
    channel_capabilities = adapter.get_capabilities()

    # 强制转换为列表
    if not attachments or not isinstance(attachments, list):
        attachments = []

    session = session_store.get_session(session_id)
    if not session:
        _log.warning(f"Session not found: {session_id}")
        server.log_message("warning", f"Session not found: {session_id}")
        return ChatResponse(error=f"Session not found: {session_id}")

    # 检查是否有图片附件
    has_image = False
    try:
        for att in attachments:
            if isinstance(att, dict):
                att_type = att.get("type", "")
                if (
                    att_type
                    and hasattr(att_type, "startswith")
                    and att_type.startswith("image/")
                ):
                    has_image = True
                    break
    except:
        attachments = []

    server.log_message(
        "info",
        f"开始生成AI回复 for session {session_id[:8]}... (附件: {len(attachments)}, 图片: {has_image})",
    )

    # 知识库检索
    knowledge_retrieved = False
    knowledge_text = ""
    knowledge_enabled = _feature_enabled(server, "knowledge", True)
    _log.info(f"[Knowledge] 知识库开关状态: {knowledge_enabled}")
    if knowledge_enabled:
        try:
            _log.info(f"[Knowledge] 开始检索，查询: {user_content[:50]}...")
            knowledge_text = server._retrieve_knowledge(user_content)
            _log.info(f"[Knowledge] 检索结果长度: {len(knowledge_text)} 字符")
            if knowledge_text:
                knowledge_retrieved = True
                _log.info("[Knowledge] 知识库检索成功")
        except Exception as e:
            _log.error(f"[Knowledge] 知识库检索失败: {e}")

    def get_response():
        try:
            # 使用深拷贝避免修改原始消息
            import copy

            messages_for_ai = copy.deepcopy(session["messages"])
            tool_call_history = None
            try:
                context_char_budget = int(
                    getattr(server, "ai_config", {}).get("max_context_length", 100000)
                )
            except (TypeError, ValueError):
                context_char_budget = 100000
            # 最低 100k token
            context_char_budget = max(100000, context_char_budget)

            prepared_context = prepare_chat_context(
                messages_for_ai,
                chat_request.content,
                knowledge_text=knowledge_text,
                max_total_chars=context_char_budget,
            )
            messages_for_ai = prepared_context.messages
            tool_call_history = prepared_context.tool_call_history
            if tool_call_history:
                _log.info(
                    f"[Continue] 检测到继续请求，恢复 {len(tool_call_history)} 条工具调用记录"
                )

            image_urls = []
            file_contents = []
            stopped_prematurely = False  # 初始化变量
            tool_messages = []  # 初始化变量

            # 支持的文本文件MIME类型
            TEXT_MIME_TYPES = [
                "text/plain",
                "application/json",
                "application/xml",
                "text/csv",
                "text/yaml",
                "application/x-yaml",
                "application/yaml",
                "text/x-python",
                "text/x-java",
                "text/x-c",
                "text/x-c++",
                "text/html",
                "text/css",
                "text/javascript",
                "application/javascript",
                "text/markdown",
                "text/x-markdown",
                "application/x-httpd-php",
                "text/x-php",
            ]

            # 根据扩展名判断是否为文本文件
            TEXT_EXTENSIONS = [
                ".txt",
                ".json",
                ".xml",
                ".csv",
                ".yaml",
                ".yml",
                ".py",
                ".java",
                ".c",
                ".cpp",
                ".h",
                ".hpp",
                ".html",
                ".css",
                ".js",
                ".ts",
                ".jsx",
                ".tsx",
                ".md",
                ".markdown",
                ".php",
                ".rb",
                ".go",
                ".rs",
                ".sh",
                ".bash",
                ".sql",
                ".ini",
                ".cfg",
                ".conf",
                ".log",
                ".env",
                ".properties",
                ".toml",
            ]

            # 创建进度卡片（所有消息都创建，立即显示"AI 正在思考..."）
            progress_card = None
            todo_card = None
            round_file_changes = []
            has_attachments = (
                attachments
                and isinstance(attachments, list)
                and len(attachments) > 0
            )

            if (
                channel_capabilities.supports_progress_updates
                and server.PROGRESS_CARD_AVAILABLE
                and server.progress_card_manager
                and server.socketio
            ):
                progress_card = server.progress_card_manager.create_card(
                    session_id=session_id,
                    parent_message_id=parent_message_id,
                    max_iterations=50,
                )
                _log.info(f"[ProgressCard] 创建进度卡片: {progress_card.card_id}")
                # 立即显示"AI 正在思考..."
                from nbot.core.progress_card import StepType

                progress_card.update(StepType.THINKING, "AI 正在思考...")

            # 创建 Todo 卡片（用于显示待办事项）
            if (
                channel_capabilities.supports_progress_updates
                and server.TODO_CARD_AVAILABLE
                and server.todo_card_manager
                and server.socketio
            ):
                todo_card = server.todo_card_manager.create_card(
                    session_id=session_id, parent_message_id=parent_message_id
                )
                _log.info(f"[TodoCard] 创建 Todo 卡片: {todo_card.card_id}")

            # 创建停止事件
            stop_event = threading.Event()
            server.stop_events[session_id] = stop_event

            if has_attachments:
                # 更新进度：开始处理附件
                if progress_card:
                    progress_card.update(
                        StepType.UPLOAD, f"正在处理 {len(attachments)} 个附件..."
                    )

                for att in attachments:
                    if isinstance(att, dict):
                        att_type = att.get("type", "")
                        att_data = att.get("data", "")
                        att_path = att.get("path", "")
                        att_name = att.get("name", "unknown")
                        att_url = att.get("url", "")



                        if isinstance(att_type, str):
                            # 图片附件 - 优先使用 data URL，其次使用文件路径
                            if att_type.startswith("image/"):
                                # 更新进度：正在识别图片
                                if progress_card:
                                    progress_card.update(
                                        StepType.IMAGE, f"正在识别图片: {att_name}"
                                    )

                                image_loaded = False
                                if att_data:
                                    image_urls.append(att_data)
                                    image_loaded = True
                                else:
                                    # 优先使用path，如果没有path则使用url
                                    path_to_use = att_path if att_path else att_url
                                    if path_to_use:
                                        # 尝试读取服务器上的图片文件
                                        try:
                                            file_path = None
                                            
                                            # 处理不同格式的路径
                                            if path_to_use.startswith("/static/"):
                                                # 静态文件路径
                                                file_path = os.path.join(
                                                    server.static_folder,
                                                    path_to_use.replace("/static/", ""),
                                                )
                                            elif "/workspace/files/" in path_to_use:
                                                # 工作区文件路径
                                                try:
                                                    # 从URL中提取文件名
                                                    filename = path_to_use.split("/workspace/files/")[-1]
                                                    
                                                    # 尝试从workspace_manager获取文件路径
                                                    workspace_available = getattr(server, 'WORKSPACE_AVAILABLE', False)
                                                    if workspace_available and server.workspace_manager:
                                                        # 从attachments中获取session_id
                                                        session_id_from_att = None
                                                        if isinstance(att, dict):
                                                            # 尝试从url中提取session_id
                                                            url = att.get("url", "")
                                                            if "/sessions/" in url:
                                                                parts = url.split("/sessions/")
                                                                if len(parts) > 1:
                                                                    session_id_from_att = parts[1].split("/")[0]
                                                        
                                                        if session_id_from_att:
                                                            file_path = server.workspace_manager.get_file_path(session_id_from_att, filename)
                                                    
                                                    # 如果还没找到，尝试使用workspace_manager获取工作区路径
                                                    if not file_path:
                                                        try:
                                                            if "/sessions/" in path_to_use:
                                                                parts = path_to_use.split("/sessions/")
                                                                if len(parts) > 1:
                                                                    session_id_from_path = parts[1].split("/")[0]
                                                                    # 使用workspace_manager获取正确的工作区路径
                                                                    if server.workspace_manager:
                                                                        ws_path = server.workspace_manager.get_workspace(session_id_from_path)
                                                                        if ws_path:
                                                                            file_path = os.path.join(ws_path, filename)
                                                                        else:
                                                                            # 工作区不存在，尝试使用get_or_create创建
                                                                            ws_path = server.workspace_manager.get_or_create(session_id_from_path, "web")
                                                                            file_path = os.path.join(ws_path, filename)
                                                                    else:
                                                                        # 没有workspace_manager，尝试直接构造路径
                                                                        file_path = os.path.join(
                                                                            server.data_dir, "workspace", f"web_{session_id_from_path[:8]}", filename
                                                                        )
                                                            else:
                                                                file_path = os.path.join(
                                                                    server.data_dir, "workspace", filename
                                                                )
                                                        except Exception:
                                                            pass
                                                except Exception:
                                                    pass
                                            else:
                                                # 其他路径，尝试直接使用
                                                file_path = path_to_use if os.path.exists(path_to_use) else None
                                            
                                            if file_path and os.path.exists(file_path):
                                                with open(file_path, "rb") as f:
                                                    import base64
                                                    file_content = f.read()
                                                    b64_data = base64.b64encode(file_content).decode("utf-8")
                                                    image_urls.append(
                                                        f"data:{att_type};base64,{b64_data}"
                                                    )
                                                    image_loaded = True
                                            else:
                                                _log.warning(
                                                    f"图片文件不存在: file_path={file_path}, 原始path/url={path_to_use}"
                                                )
                                        except Exception as e:
                                            _log.warning(
                                                f"读取图片文件失败: {att_name}, {e}"
                                            )
                                # 无论成功还是失败，都更新完成状态
                                if progress_card:
                                    if image_loaded:
                                        progress_card.update(
                                            StepType.IMAGE_DONE,
                                            f"图片已加载: {att_name}",
                                            True,
                                        )
                                    else:
                                        progress_card.update(
                                            StepType.IMAGE_DONE,
                                            f"图片加载失败: {att_name}",
                                            False,
                                        )

                            # 文本文件 - 优先使用 data URL，其次使用文件路径
                            elif att_type in TEXT_MIME_TYPES:
                                # 更新进度：正在读取文件
                                if progress_card:
                                    progress_card.update(
                                        StepType.FILE, f"正在读取文件: {att_name}"
                                    )

                                text_content = None
                                # 从 data URL 提取内容
                                if att_data and att_data.startswith("data:"):
                                    try:
                                        import base64

                                        b64_data = (
                                            att_data.split(",")[1]
                                            if "," in att_data
                                            else att_data
                                        )
                                        text_content = base64.b64decode(
                                            b64_data
                                        ).decode("utf-8", errors="ignore")
                                    except Exception as e:
                                        _log.warning(
                                            f"提取文本文件失败: {att_name}, {e}"
                                        )
                                # 从文件路径读取内容
                                elif att_path:
                                    try:
                                        file_path = os.path.join(
                                            server.static_folder,
                                            att_path.replace("/static/", ""),
                                        )
                                        if os.path.exists(file_path):
                                            with open(
                                                file_path,
                                                "r",
                                                encoding="utf-8",
                                                errors="ignore",
                                            ) as f:
                                                text_content = f.read()
                                    except Exception as e:
                                        _log.warning(
                                            f"读取文件失败: {att_name}, {e}"
                                        )

                                if text_content:
                                    file_contents.append(
                                        f"【文件 {att_name} 内容】:\n{text_content[:10000]}"
                                    )
                                    # 文件处理完成
                                    if progress_card:
                                        progress_card.update(
                                            StepType.FILE_DONE,
                                            f"文件已读取: {att_name}",
                                            True,
                                        )
                                else:
                                    # 文件处理失败
                                    if progress_card:
                                        progress_card.update(
                                            StepType.FILE_DONE,
                                            f"文件读取失败: {att_name}",
                                            False,
                                        )

                            # 根据扩展名判断是否为文本文件
                            elif any(
                                att_name.lower().endswith(ext)
                                for ext in TEXT_EXTENSIONS
                            ):
                                # 更新进度：正在读取文件
                                if progress_card:
                                    progress_card.update(
                                        StepType.FILE, f"正在读取文件: {att_name}"
                                    )

                                text_content = None
                                file_read_success = False

                                # 从 data URL 提取内容
                                if att_data and att_data.startswith("data:"):
                                    try:
                                        import base64

                                        b64_data = (
                                            att_data.split(",")[1]
                                            if "," in att_data
                                            else att_data
                                        )
                                        text_content = base64.b64decode(
                                            b64_data
                                        ).decode("utf-8", errors="ignore")
                                        file_read_success = True
                                    except Exception as e:
                                        _log.warning(
                                            f"提取文本文件失败: {att_name}, {e}"
                                        )
                                # 从文件路径读取内容
                                elif att_path:
                                    try:
                                        file_path = os.path.join(
                                            server.static_folder,
                                            att_path.replace("/static/", ""),
                                        )
                                        if os.path.exists(file_path):
                                            with open(
                                                file_path,
                                                "r",
                                                encoding="utf-8",
                                                errors="ignore",
                                            ) as f:
                                                text_content = f.read()
                                                file_read_success = True
                                    except Exception as e:
                                        _log.warning(
                                            f"读取文件失败: {att_name}, {e}"
                                        )

                                if text_content:
                                    file_contents.append(
                                        f"【文件 {att_name} 内容】:\n{text_content[:10000]}"
                                    )

                                # 更新完成状态
                                if progress_card:
                                    if file_read_success:
                                        progress_card.update(
                                            StepType.FILE_DONE,
                                            f"文件已读取: {att_name}",
                                            True,
                                        )
                                    else:
                                        progress_card.update(
                                            StepType.FILE_DONE,
                                            f"文件读取失败: {att_name}",
                                            False,
                                        )

                            # 工作区文件 - 从 URL 中提取路径并读取内容
                            elif att_type == "workspace/file" or (
                                att_path and "/workspace/files/" in str(att_path)
                            ):
                                # 更新进度：正在读取工作区文件
                                if progress_card:
                                    progress_card.update(
                                        StepType.FILE,
                                        f"正在读取工作区文件: {att_name}",
                                    )

                                text_content = None
                                file_read_success = False

                                # 尝试从 URL 中提取工作区文件路径
                                try:
                                    import re

                                    url = att.get("url", "")
                                    # 匹配 /api/sessions/{session_id}/workspace/files/{path}
                                    match = re.search(
                                        r"/workspace/files/([^?]+)", url
                                    )
                                    if match:
                                        ws_file_path = match.group(1)
                                        if (
                                            server.WORKSPACE_AVAILABLE
                                            and server.workspace_manager
                                        ):
                                            # 使用 server.workspace_manager 读取文件
                                            ws_path = (
                                                server.workspace_manager.get_workspace(
                                                    session_id
                                                )
                                            )
                                            if ws_path:
                                                full_path = os.path.join(
                                                    ws_path, ws_file_path
                                                )
                                                if os.path.exists(
                                                    full_path
                                                ) and os.path.isfile(full_path):
                                                    with open(
                                                        full_path,
                                                        "r",
                                                        encoding="utf-8",
                                                        errors="ignore",
                                                    ) as f:
                                                        text_content = f.read()
                                                        file_read_success = True
                                                        _log.info(
                                                            f"工作区文件已读取: {ws_file_path}"
                                                        )
                                except Exception as e:
                                    _log.warning(
                                        f"读取工作区文件失败: {att_name}, {e}"
                                    )

                                if text_content:
                                    file_contents.append(
                                        f"【文件 {att_name} 内容】:\n{text_content[:10000]}"
                                    )

                                # 更新完成状态
                                if progress_card:
                                    if file_read_success:
                                        progress_card.update(
                                            StepType.FILE_DONE,
                                            f"工作区文件已读取: {att_name}",
                                            True,
                                        )
                                    else:
                                        progress_card.update(
                                            StepType.FILE_DONE,
                                            f"工作区文件读取失败: {att_name}",
                                            False,
                                        )

                            # 使用本地解析器解析的文件类型
                            elif (
                                any(
                                    att_name.lower().endswith(ext)
                                    for ext in [
                                        ".pdf",
                                        ".doc",
                                        ".docx",
                                        ".ppt",
                                        ".pptx",
                                        ".xls",
                                        ".xlsx",
                                    ]
                                )
                                and att_path
                            ):
                                try:
                                    file_abs_path = os.path.join(
                                        server.static_folder,
                                        att_path.replace("/static/", ""),
                                    )
                                    if os.path.exists(file_abs_path):
                                        # 使用文件解析器获取元数据
                                        if server.FILE_PARSER_AVAILABLE and server.file_parser:
                                            metadata = (
                                                server.file_parser.get_file_metadata(
                                                    file_abs_path, att_name
                                                )
                                            )
                                            if metadata.get("success"):
                                                # 构建元数据信息
                                                meta_parts = [f"文件: {att_name}"]
                                                meta_parts.append(
                                                    f"类型: {metadata.get('type', 'unknown')}"
                                                )
                                                meta_parts.append(
                                                    f"大小: {metadata.get('size_str', 'unknown')}"
                                                )

                                                # 添加额外信息（页数、工作表数等）
                                                if "pages" in metadata:
                                                    meta_parts.append(
                                                        f"页数: {metadata['pages']}"
                                                    )
                                                if "slides" in metadata:
                                                    meta_parts.append(
                                                        f"幻灯片数: {metadata['slides']}"
                                                    )
                                                if "sheets" in metadata:
                                                    meta_parts.append(
                                                        f"工作表数: {metadata['sheets']}"
                                                    )
                                                    if "sheet_names" in metadata:
                                                        sheet_names = metadata[
                                                            "sheet_names"
                                                        ]
                                                        if isinstance(
                                                            sheet_names, list
                                                        ):
                                                            meta_parts.append(
                                                                f"工作表: {', '.join(str(s) for s in sheet_names)}"
                                                            )
                                                if "paragraphs" in metadata:
                                                    meta_parts.append(
                                                        f"段落数: {metadata['paragraphs']}"
                                                    )
                                                if "tables" in metadata:
                                                    meta_parts.append(
                                                        f"表格数: {metadata['tables']}"
                                                    )

                                                meta_parts.append(
                                                    "\n如需查看文件内容，请调用 workspace_parse_file 工具解析此文件。"
                                                )

                                                file_contents.append(
                                                    "【文件元数据】\n"
                                                    + "\n".join(meta_parts)
                                                )
                                                _log.info(
                                                    f"文件元数据已提取: {att_name}"
                                                )
                                            else:
                                                file_contents.append(
                                                    f"【文件 {att_name}】类型: {att_type} (无法获取元数据)"
                                                )
                                        else:
                                            file_contents.append(
                                                f"【文件 {att_name}】类型: {att_type} (文件解析器不可用)"
                                            )
                                    else:
                                        file_contents.append(
                                            f"【文件 {att_name}】类型: {att_type} (文件不存在)"
                                        )
                                except Exception as e:
                                    _log.warning(
                                        f"获取文件元数据失败: {att_name}, {e}"
                                    )
                                    file_contents.append(
                                        f"【文件 {att_name}】类型: {att_type} (获取元数据失败)"
                                    )
                            # 其他文件 - 告知AI文件类型
                            elif att_type:
                                file_contents.append(
                                    f"【文件 {att_name}】类型: {att_type} (暂不支持解析)"
                                )

            # 附件处理完成，更新进度
            if progress_card:
                _log.info(
                    f"[ProgressCard] 准备更新 UPLOAD_DONE，当前步骤数: {len(progress_card.steps)}"
                )
                progress_card.update(
                    StepType.UPLOAD_DONE,
                    f"附件处理完成 ({len(attachments)} 个文件)",
                    True,
                )
                _log.info(
                    f"[ProgressCard] UPLOAD_DONE 更新完成，步骤数: {len(progress_card.steps)}"
                )

            # 知识库检索成功，更新进度
            if progress_card and knowledge_retrieved:
                _log.info("[ProgressCard] 准备更新 KNOWLEDGE 步骤")
                progress_card.update(StepType.KNOWLEDGE, "📚 知识库检索...")
                progress_card.update(
                    StepType.KNOWLEDGE_DONE, "📚 知识库已加载", True
                )
                _log.info("[ProgressCard] KNOWLEDGE 步骤更新完成")

            # 合并文件内容到用户消息
            enhanced_content = user_content
            if file_contents:
                enhanced_content = (
                    user_content + "\n\n" + "\n\n".join(file_contents)
                )

            # 定义默认的完成函数（用于多模态AI分支）
            def complete_thinking_card():
                """将进度卡片标记为完成状态"""
                if progress_card:
                    try:
                        from nbot.core.progress_card import StepType

                        progress_card.update(StepType.DONE, "✅ 处理完成", True)
                        progress_card.complete()
                    except Exception as e:
                        _log.warning(f"[ThinkingCard] 标记完成失败: {e}")

            # 检查是否有图片附件，如果有则使用多模态AI进行图片识别
            image_recognition_result = None
            if image_urls:
                # 使用多模态AI处理图片，获取图片识别结果
                image_recognition_result = server._get_ai_response_with_images(
                    messages_for_ai, image_urls, enhanced_content
                )
                
                # 更新进度卡片，保存图片识别结果
                if progress_card:
                    try:
                        from nbot.core.progress_card import StepType
                        # 更新图片处理步骤，添加识别结果
                        for step in reversed(progress_card.steps):
                            if step['type'] == 'image' and step['status'] == 'done':
                                step['full_result'] = image_recognition_result
                                step['detail'] = f"图片识别完成 ({len(image_recognition_result)} 字符)"
                                break
                        progress_card._emit_update()
                    except Exception:
                        pass
            
            # 如果有图片识别结果，将其加入对话上下文
            if image_recognition_result:
                # 构建增强的用户消息，包含图片识别结果
                enhanced_user_content = user_content
                if user_content:
                    enhanced_user_content = f"{user_content}\n\n[图片内容描述]\n{image_recognition_result}"
                else:
                    enhanced_user_content = f"[图片内容描述]\n{image_recognition_result}"
                
                # 更新messages_for_ai，将图片识别结果加入上下文
                if messages_for_ai and messages_for_ai[-1].get("role") == "user":
                    messages_for_ai[-1]["content"] = enhanced_user_content            
            # 继续正常的对话流程（工具调用或普通对话）
            if False:  # 占位，实际逻辑在下面
                pass  # 这个分支不会执行，只是为了保持代码结构
            else:
                # 尝试使用工具调用（多轮）
                try:
                    from nbot.services.tools import get_enabled_tools, execute_tool

                    supports_tools = server.ai_config.get("supports_tools", True)
                    enabled_tools = get_enabled_tools() if supports_tools else []
                    # 记录加载的工具列表
                    tool_names = [
                        t.get("function", {}).get("name", "unknown")
                        for t in enabled_tools
                    ]
                    if not supports_tools:
                        _log.info("[Tools] 当前模型配置声明不支持工具调用，跳过工具链")
                    _log.info(
                        f"[Tools] 已加载 {len(enabled_tools)} 个工具: {tool_names}"
                    )
                    if enabled_tools:
                        # 构建工具上下文（包含 session_id）
                        tool_context = {
                            "session_id": session_id,
                            "session_type": session.get("type", "unknown"),
                            "user_id": session.get("user_id", session_id),
                        }

                        # 构建多轮消息（使用深拷贝）
                        tool_messages = copy.deepcopy(messages_for_ai)
                        if (
                            file_contents
                            and tool_messages
                            and tool_messages[-1].get("role") == "user"
                        ):
                            tool_messages[-1]["content"] = enhanced_content

                        max_iterations = 50
                        consecutive_errors = 0
                        max_consecutive_errors = 3
                        final_content = None

                        _log.info(
                            f"[ThinkingCard] 使用 ProgressCard 系统, session_id={session_id}, card_id={progress_card.card_id if progress_card else 'None'}"
                        )

                        def update_thinking_card(
                            step_type,
                            step_name,
                            step_detail=None,
                            step_result=None,
                            step_arguments=None,
                            step_full_result=None,
                            thinking_content=None,
                        ):
                            """更新进度卡片 - 使用新的 ProgressCard 系统"""
                            if not progress_card:
                                return

                            try:
                                from nbot.core.progress_card import StepType

                                step_type_map = {
                                    "start": StepType.START,
                                    "thinking": StepType.THINKING,
                                    "ai_thinking": StepType.AI_THINKING,
                                    "tool": StepType.TOOL,
                                    "tool_done": StepType.TOOL_DONE,
                                    "image": StepType.IMAGE,
                                    "image_done": StepType.IMAGE_DONE,
                                    "file": StepType.FILE,
                                    "file_done": StepType.FILE_DONE,
                                    "upload": StepType.UPLOAD,
                                    "upload_done": StepType.UPLOAD_DONE,
                                    "knowledge": StepType.KNOWLEDGE,
                                    "knowledge_done": StepType.KNOWLEDGE_DONE,
                                }
                                step_type_enum = step_type_map.get(step_type)
                                if step_type_enum:
                                    progress_card.update(
                                        step_type_enum,
                                        step_name,
                                        step_detail,
                                        step_result,
                                        step_arguments,
                                        step_full_result,
                                        thinking_content,
                                    )
                            except Exception as e:
                                _log.warning(f"[ThinkingCard] 更新进度失败: {e}")

                        def update_thinking_stream(thinking_content):
                            """流式更新AI思考内容"""
                            if not progress_card:
                                return
                            try:
                                progress_card.append_thinking_content(
                                    thinking_content
                                )
                            except Exception as e:
                                _log.warning(
                                    f"[ThinkingCard] 流式更新思考内容失败: {e}"
                                )

                        # 注意：complete_thinking_card 函数已在前面定义

                        def send_workspace_file_message(file_path, filename):
                            if (
                                channel_capabilities is not None
                                and not channel_capabilities.supports_file_send
                            ):
                                _log.info(
                                    f"[SendFile] channel does not support file sending: session_id={session_id}"
                                )
                                return
                            _log.info(
                                f"[SendFile] \u51c6\u5907\u53d1\u9001\u6587\u4ef6: {filename}, path={file_path}, session_id={session_id}"
                            )
                            if not file_path or not filename or not session_id:
                                _log.warning(
                                    f"[SendFile] \u65e0\u6cd5\u53d1\u9001\u6587\u4ef6: file_path={file_path}, filename={filename}, session_id={session_id}"
                                )
                                return

                            try:
                                import base64
                                import mimetypes
                                import shutil

                                if not os.path.exists(file_path):
                                    _log.error(f"[SendFile] \u6587\u4ef6\u4e0d\u5b58\u5728: {file_path}")
                                    return

                                file_size = os.path.getsize(file_path)
                                mime_type, _ = mimetypes.guess_type(file_path)
                                if not mime_type:
                                    mime_type = "application/octet-stream"
                                ext = os.path.splitext(file_path)[1].lower()
                                is_image = mime_type.startswith("image/")

                                files_dir = os.path.join(server.static_folder, "files")
                                os.makedirs(files_dir, exist_ok=True)
                                file_hash = hashlib.md5(
                                    f"{file_path}{time.time()}".encode()
                                ).hexdigest()[:8]
                                safe_name = f"{file_hash}_{filename}"
                                dest_path = os.path.join(files_dir, safe_name)
                                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                                shutil.copy2(file_path, dest_path)
                                download_url = f"/static/files/{safe_name}"

                                file_info = {
                                    "id": str(uuid.uuid4()),
                                    "role": "assistant",
                                    "content": f"[\u6587\u4ef6: {filename}]",
                                    "timestamp": datetime.now().isoformat(),
                                    "sender": "AI",
                                    "source": "web",
                                    "session_id": session_id,
                                    "file": {
                                        "name": filename,
                                        "type": mime_type,
                                        "size": file_size,
                                        "is_image": is_image,
                                        "extension": ext,
                                        "download_url": download_url,
                                        "url": download_url,
                                    },
                                }

                                if is_image and file_size < 5 * 1024 * 1024:
                                    try:
                                        with open(file_path, "rb") as f:
                                            file_data = f.read()
                                        b64_data = base64.b64encode(file_data).decode(
                                            "utf-8"
                                        )
                                        file_info["file"][
                                            "data"
                                        ] = f"data:{mime_type};base64,{b64_data}"
                                        file_info["file"]["preview_url"] = file_info[
                                            "file"
                                        ]["data"]
                                    except Exception as img_err:
                                        _log.warning(
                                            f"[SendFile] \u56fe\u7247\u8f6c base64 \u5931\u8d25: {img_err}"
                                        )

                                if session_id in server.sessions:
                                    session_store.append_message(session_id, file_info)

                                server.socketio.emit(
                                    "new_message", file_info, room=session_id
                                )
                                _log.info(
                                    f"[SendFile] \u6587\u4ef6\u5df2\u53d1\u9001: {filename} ({mime_type}, {file_size} bytes)"
                                )
                            except Exception as send_err:
                                _log.error(
                                    f"[SendFile] \u53d1\u9001\u6587\u4ef6\u65f6\u51fa\u9519: {send_err}",
                                    exc_info=True,
                                )

                        def get_tool_display_name(tool_name):
                            return {
                                "search_news": "\U0001F50D \u641c\u7d22\u65b0\u95fb",
                                "get_weather": "\U0001F324\uFE0F \u67e5\u8be2\u5929\u6c14",
                                "search_web": "\U0001F310 \u7f51\u9875\u641c\u7d22",
                                "get_date_time": "\U0001F550 \u83b7\u53d6\u65f6\u95f4",
                                "http_get": "\U0001F4E1 \u83b7\u53d6\u7f51\u9875",
                                "understand_image": "\U0001F5BC\uFE0F \u7406\u89e3\u56fe\u7247",
                                "workspace_create_file": "\U0001F4DD \u521b\u5efa\u6587\u4ef6",
                                "workspace_read_file": "\U0001F4D6 \u8bfb\u53d6\u6587\u4ef6",
                                "workspace_edit_file": "\u270F\uFE0F \u7f16\u8f91\u6587\u4ef6",
                                "workspace_delete_file": "\U0001F5D1\uFE0F \u5220\u9664\u6587\u4ef6",
                                "workspace_list_files": "\U0001F4C1 \u5217\u51fa\u6587\u4ef6",
                                "workspace_tree": "\U0001F333 \u663e\u793a\u76ee\u5f55\u6811",
                                "workspace_send_file": "\U0001F4E4 \u53d1\u9001\u6587\u4ef6",
                                "todo_add": "\u2705 \u6dfb\u52a0\u5f85\u529e",
                                "todo_list": "\U0001F4CB \u5217\u51fa\u5f85\u529e",
                                "todo_complete": "\u2713 \u5b8c\u6210\u5f85\u529e",
                                "todo_delete": "\U0001F5D1\uFE0F \u5220\u9664\u5f85\u529e",
                                "todo_clear": "\U0001F9F9 \u6e05\u7a7a\u5f85\u529e",
                            }.get(tool_name, f"\u2699\uFE0F {tool_name}")

                        def execute_web_tool(
                            tool_call, thinking_content, iteration, current_tool_messages
                        ):
                            result = execute_tool(
                                tool_call["name"],
                                tool_call["arguments"],
                                context=tool_context,
                            )
                            # 检查是否需要用户确认（exec_command 非白名单）
                            if result.get('require_confirmation'):
                                request_id = result.get('request_id', '')
                                command = result.get('command', '')
                                _log.info(f"[Web Confirm] 请求确认: request_id={request_id[:8] if request_id else '?'}, cmd={command[:80]}")
                                server.socketio.emit('exec_confirm_request', {
                                    'request_id': request_id,
                                    'command': command,
                                    'message': result.get('message', ''),
                                    'session_id': session_id,
                                }, room=session_id)
                                raise ToolLoopExit(
                                    f"命令 `{command}` 需要您的确认，请在弹窗中操作。\n[请求ID: {request_id[:8] if request_id else 'N/A'}]"
                                )
                            return result

                        def on_tool_start(
                            tool_call, ai_thinking_content, iteration, current_tool_messages
                        ):
                            tool_name = tool_call["name"]
                            arguments = tool_call["arguments"]
                            tool_display_name = get_tool_display_name(tool_name)
                            update_thinking_card(
                                "tool",
                                tool_display_name,
                                json.dumps(arguments, ensure_ascii=False)[:100],
                                None,
                                arguments,
                                None,
                                ai_thinking_content if ai_thinking_content else None,
                            )

                            if tool_name.startswith("workspace_"):
                                args_str = json.dumps(arguments, ensure_ascii=False)
                                _log.info(f"[Workspace] \u5de5\u5177\u8c03\u7528: {tool_name}")
                                if len(args_str) > 200:
                                    _log.info(
                                        f"[Workspace] \u53c2\u6570: {args_str[:200]}... ({len(args_str)} \u5b57\u7b26)"
                                    )
                                else:
                                    _log.info(f"[Workspace] \u53c2\u6570: {args_str}")

                        def on_tool_result(
                            tool_call,
                            tool_result,
                            ai_thinking_content,
                            iteration,
                            current_tool_messages,
                        ):
                            tool_name = tool_call["name"]
                            arguments = tool_call["arguments"]
                            tool_display_name = get_tool_display_name(tool_name)

                            if tool_result.get("success"):
                                file_changes = tool_result.get("file_changes") or []
                                if file_changes:
                                    round_file_changes.extend(file_changes)
                                    result_preview = "；".join(
                                        f"{change.get('action', 'changed')}: {change.get('path', '')}"
                                        for change in file_changes[:3]
                                    )[:160]
                                else:
                                    result_preview = str(
                                        tool_result.get(
                                            "content", tool_result.get("files", tool_result)
                                        )
                                    )[:100]
                                update_thinking_card(
                                    "tool_done",
                                    tool_display_name,
                                    result_preview,
                                    None,
                                    step_arguments=arguments,
                                    step_full_result=tool_result,
                                    thinking_content=ai_thinking_content
                                    if ai_thinking_content
                                    else None,
                                )
                            else:
                                update_thinking_card(
                                    "tool_done",
                                    tool_display_name,
                                    None,
                                    None,
                                    step_arguments=arguments,
                                    step_full_result=tool_result,
                                    thinking_content=ai_thinking_content
                                    if ai_thinking_content
                                    else None,
                                )

                            if tool_name.startswith("workspace_"):
                                if tool_result.get("success"):
                                    _log.info(f"[Workspace] \u6267\u884c\u6210\u529f: {tool_name}")
                                    _log.info(
                                        f"[Workspace] \u7ed3\u679c\u9884\u89c8: {str(tool_result)[:300]}"
                                    )
                                else:
                                    _log.error(
                                        f"[Workspace] \u6267\u884c\u5931\u8d25: {tool_name} - {tool_result.get('error')}"
                                    )

                            if tool_name.startswith("todo_"):
                                if tool_result.get("success"):
                                    _log.info(
                                        f"[Todo] \u6267\u884c\u6210\u529f: {tool_name} - {tool_result.get('message', '')}"
                                    )
                                    if todo_card and server.TODO_CARD_AVAILABLE:
                                        try:
                                            if tool_name == "todo_add":
                                                todo_info = tool_result.get("todo", {})
                                                todo_card.add_todo(
                                                    todo_id=todo_info.get("id"),
                                                    content=todo_info.get("content", ""),
                                                    priority=todo_info.get(
                                                        "priority", "medium"
                                                    ),
                                                )
                                            elif tool_name == "todo_complete":
                                                todo_info = tool_result.get("todo", {})
                                                todo_card.complete_todo(
                                                    todo_info.get("id")
                                                )
                                            elif tool_name == "todo_delete":
                                                deleted_todo = tool_result.get(
                                                    "deleted_todo", {}
                                                )
                                                todo_card.delete_todo(
                                                    deleted_todo.get("id")
                                                )
                                            elif tool_name == "todo_list":
                                                todo_card.update_todos(
                                                    tool_result.get("todos", [])
                                                )
                                            elif tool_name == "todo_clear":
                                                todo_card.todos = []
                                                todo_card._emit_update()
                                        except Exception as e:
                                            _log.warning(
                                                f"[TodoCard] \u66f4\u65b0 Todo \u5361\u7247\u5931\u8d25: {e}"
                                            )
                                else:
                                    _log.error(
                                        f"[Todo] \u6267\u884c\u5931\u8d25: {tool_name} - {tool_result.get('error', '')}"
                                    )

                            if (
                                tool_name == "send_message"
                                and tool_result.get("action") == "send_message"
                            ):
                                progress_msg = {
                                    "id": str(uuid.uuid4()),
                                    "role": "assistant",
                                    "content": tool_result.get("content", ""),
                                    "timestamp": datetime.now().isoformat(),
                                    "sender": "AI",
                                    "message_type": tool_result.get(
                                        "message_type", "progress"
                                    ),
                                    "is_progress_message": True,
                                }
                                server.socketio.emit(
                                    "progress_message",
                                    {"session_id": session_id, "message": progress_msg},
                                    room=session_id,
                                )
                                _log.info(
                                    f"[SendMessage] \u5df2\u53d1\u9001\u8fdb\u5ea6\u6d88\u606f: {tool_result.get('content', '')[:50]}..."
                                )
                                return {
                                    "role": "tool",
                                    "tool_call_id": tool_call.get("id", ""),
                                    "content": json.dumps(
                                        {
                                            "success": True,
                                            "message": "\u6d88\u606f\u5df2\u53d1\u9001",
                                        },
                                        ensure_ascii=False,
                                    ),
                                }

                            if (
                                tool_name == "workspace_send_file"
                                and tool_result.get("action") == "send_file"
                            ):
                                send_workspace_file_message(
                                    tool_result.get("path", ""),
                                    tool_result.get("filename", ""),
                                )

                            return None

                        hooks = ToolLoopHooks(
                            on_iteration_start=lambda iteration, current_tool_messages: (
                                progress_card.increment_iteration()
                                if progress_card
                                else None
                            ),
                            on_tool_start=on_tool_start,
                            on_tool_result=on_tool_result,
                        )

                        execution_result = run_tool_loop_session(
                            ToolLoopSession(
                                initial_messages=tool_messages,
                                model_call=lambda current_messages, stop_event=None: server._get_ai_response_with_tools(
                                    current_messages,
                                    enabled_tools,
                                    stop_event=stop_event,
                                ),
                                tool_executor=execute_web_tool,
                                tool_call_history=tool_call_history,
                                max_iterations=max_iterations,
                                max_consecutive_errors=max_consecutive_errors,
                                stop_event=stop_event,
                                hooks=hooks,
                            )
                        )
                        loop_result = execution_result.loop_result
                        stopped_prematurely = loop_result.stopped
                        final_content = loop_result.final_content
                        tool_messages = loop_result.tool_messages
                        current_round_tool_trace = extract_tool_call_history(
                            tool_messages[len(execution_result.prepared_messages) :]
                        )
                        consecutive_errors = loop_result.consecutive_errors

                        if not final_content:
                            _log.warning("[Tools] AI 未生成最终回复，使用默认提示")
                            final_content = (
                                "抱歉，处理过程中出现了问题，请稍后再试~"
                            )

                        _log.info(f"[Tools] 最终回复长度: {len(final_content)}")

                        # 检查是否为等待确认状态（exec_command 非白名单）
                        if final_content and '[请求ID:' in final_content:
                            # 命令等待用户确认，不标记完成，不发送 AI 消息
                            _log.info("[Tools] 等待用户确认命令执行，保持进度卡片等待状态")
                            # 进度卡片已在 execute_web_tool 中更新为等待状态，直接跳过后续处理
                        else:
                            # 将进度卡片标记为完成（不再删除）
                            complete_thinking_card()
                    else:
                        # 无可用工具，使用普通 AI 调用
                        if (
                            file_contents
                            and messages_for_ai
                            and messages_for_ai[-1].get("role") == "user"
                        ):
                            messages_for_ai[-1]["content"] = enhanced_content
                        final_content = server._get_ai_response(messages_for_ai)
                        # 完成进度卡片
                        complete_thinking_card()
                except ImportError:
                    # 工具模块不可用，使用普通 AI 调用
                    if (
                        file_contents
                        and messages_for_ai
                        and messages_for_ai[-1].get("role") == "user"
                    ):
                        messages_for_ai[-1]["content"] = enhanced_content
                    final_content = server._get_ai_response(messages_for_ai)
                    # 完成进度卡片
                    complete_thinking_card()
                except Exception as e:
                    _log.warning(
                        f"Tool calling error: {e}, falling back to normal AI"
                    )
                    if (
                        file_contents
                        and messages_for_ai
                        and messages_for_ai[-1].get("role") == "user"
                    ):
                        messages_for_ai[-1]["content"] = enhanced_content
                    final_content = server._get_ai_response(messages_for_ai)
                    # 完成进度卡片
                    complete_thinking_card()

            # 如果是提前停止，保存工具调用历史并添加继续按钮
            if stopped_prematurely:
                tool_call_history = extract_tool_call_history(tool_messages)
                _log.info(
                    f"[Stop] 保存工具调用历史，共 {len(tool_call_history)} 条记录"
                )

                assistant_message = _build_channel_assistant_message(
                    build_continue_chat_response(tool_trace=tool_call_history),
                    session_id=session_id,
                    adapter=adapter,
                )
                session_store.append_message(session_id, assistant_message)
                complete_thinking_card()
                if channel_capabilities.supports_stream:
                    server._stream_send_response(session_id, assistant_message)
                else:
                    server.socketio.emit(
                        "ai_response",
                        {"session_id": session_id, "message": assistant_message},
                        room=session_id,
                    )
                _emit_change_card(
                    server,
                    session_store,
                    session_id=session_id,
                    parent_message_id=parent_message_id,
                    file_changes=round_file_changes,
                )
                return

            # 如果是等待用户确认状态，不发送 AI 消息，直接返回
            if final_content and '[请求ID:' in final_content:
                _log.info("[Tools] 命令等待用户确认中，跳过 AI 响应发送")
                # 清理事件上下文，但不标记完成
                if progress_card:
                    progress_card._emit_update()
                return

            assistant_content = final_content

            assistant_message = _build_channel_assistant_message(
                ChatResponse(
                    final_content=assistant_content,
                    tool_trace=current_round_tool_trace
                    if "current_round_tool_trace" in locals()
                    else [],
                ),
                session_id=session_id,
                adapter=adapter,
            )

            # 使用流式发送
            _log.info("[Stream] _trigger_ai_response 准备发送流式响应")
            if channel_capabilities.supports_stream:
                server._stream_send_response(session_id, assistant_message)
            else:
                server.socketio.emit(
                    "ai_response",
                    {"session_id": session_id, "message": assistant_message},
                    room=session_id,
                )

            session_store.append_message(session_id, assistant_message)
            try:
                from nbot.web.routes.push import send_web_push

                send_web_push(
                    server,
                    title="NekoBot",
                    body=assistant_content[:160],
                    url=f"/?session_id={session_id}",
                    session_id=session_id,
                    tag=f"nekobot-session-{session_id}",
                )
            except Exception as push_error:
                _log.warning("Failed to send Web Push notification: %s", push_error)
            _emit_change_card(
                server,
                session_store,
                session_id=session_id,
                parent_message_id=parent_message_id,
                file_changes=round_file_changes,
            )

            # 更新 Token 统计
            estimated_tokens = len(chat_request.content) + len(assistant_content)
            input_tokens = len(chat_request.content)
            output_tokens = len(assistant_content)
            server.token_stats["today"] = (
                server.token_stats.get("today", 0) + estimated_tokens
            )
            server.token_stats["month"] = (
                server.token_stats.get("month", 0) + estimated_tokens
            )

            # 更新历史记录
            today_str = datetime.now().strftime("%Y-%m-%d")
            history = server.token_stats.get("history", [])
            if not history or history[-1].get("date") != today_str:
                history.append(
                    {
                        "date": today_str,
                        "input": input_tokens,
                        "output": output_tokens,
                        "total": estimated_tokens,
                        "cost": 0.0,
                        "message_count": 1,
                    }
                )
            else:
                history[-1]["input"] = history[-1].get("input", 0) + input_tokens
                history[-1]["output"] = history[-1].get("output", 0) + output_tokens
                history[-1]["total"] = (
                    history[-1].get("total", 0) + estimated_tokens
                )
                history[-1]["message_count"] = (
                    history[-1].get("message_count", 0) + 1
                )
            server.token_stats["history"] = history[-30:]

            # 更新会话统计
            if session_id:
                sessions_stats = server.token_stats.get("sessions", {})
                if session_id not in sessions_stats:
                    sessions_stats[session_id] = {
                        "input": 0,
                        "output": 0,
                        "total": 0,
                        "message_count": 0,
                    }
                sessions_stats[session_id]["input"] = (
                    sessions_stats[session_id].get("input", 0) + input_tokens
                )
                sessions_stats[session_id]["output"] = (
                    sessions_stats[session_id].get("output", 0) + output_tokens
                )
                sessions_stats[session_id]["total"] = (
                    sessions_stats[session_id].get("total", 0) + estimated_tokens
                )
                sessions_stats[session_id]["message_count"] = (
                    sessions_stats[session_id].get("message_count", 0) + 2
                )  # 用户消息 + AI回复
                server.token_stats["sessions"] = sessions_stats

            # 注意：ai_response 现在通过 _stream_send_response 流式发送了，不再单独发送

            # 记录日志
            server.log_message(
                "info",
                f"AI回复完成 for session {session_id[:8]}, tokens: {estimated_tokens}",
            )

            # 保存会话和 Token 统计到磁盘
            server._save_data("token_stats")

            # 自动重命名会话（如果是默认名称且对话轮数达到一定数量）
            try:
                current_name = session.get("name", "")
                message_count = len(session.get("messages", []))
                _log.info(
                    f"[SessionRename] 检查重命名条件: name='{current_name}', count={message_count}"
                )
                # 如果是默认名称（以"会话"开头或是空名称），且已有至少4条消息（2轮对话）
                if (
                    current_name.startswith("会话")
                    or not current_name
                    or current_name.startswith("Web 会话")
                ) and message_count >= 4:
                    _log.info("[SessionRename] 条件满足，开始生成新名称...")
                    new_name = server._generate_session_name(
                        session["messages"],
                        session_id=session_id,
                        parent_message_id=assistant_message["id"],
                    )
                    if new_name:
                        session["name"] = new_name
                        server._save_data("sessions")
                        # 通知前端会话名称已更新
                        server.socketio.emit(
                            "session_renamed",
                            {"session_id": session_id, "name": new_name},
                            room=session_id,
                        )
                        _log.info(
                            f"[SessionRename] 会话 {session_id[:8]}... 已重命名为: {new_name}"
                        )
                    else:
                        _log.warning("[SessionRename] 生成名称失败，返回 None")
                else:
                    _log.info(
                        f"[SessionRename] 条件不满足: startswith={current_name.startswith('会话')}, empty={not current_name}, count={message_count}"
                    )
            except Exception as e:
                _log.error(f"[SessionRename] 自动重命名失败: {e}", exc_info=True)

        except Exception as e:
            _log.error(f"Error in AI response: {e}")
            error_message = _build_channel_assistant_message(
                ChatResponse(error=f"抱歉，处理消息时出错: {str(e)}"),
                session_id=session_id,
                adapter=adapter,
            )
            session_store.append_message(session_id, error_message)
            server.socketio.emit(
                "ai_response",
                {"session_id": session_id, "message": error_message},
                room=session_id,
            )
        finally:
            # 清理停止事件
            if session_id in server.stop_events:
                del server.stop_events[session_id]
                _log.info(f"[Stop] 清理停止事件: {session_id}")

    # 使用 Flask-SocketIO 的后台任务机制
    server.socketio.start_background_task(get_response)
    return ChatResponse(metadata={"scheduled": True, "session_id": session_id})


def get_ai_response(self, messages: List[Dict]) -> str:
    """获取 AI 回复"""
    if not self.ai_client:
        _log.warning("AI client not initialized")
        return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

    try:
        response = self.ai_client.chat_completion(
            model=self.ai_model, messages=messages, stream=False
        )

        # 检查 choices 是否有效
        if not response.choices or len(response.choices) == 0:
            base_resp = getattr(response, "base_resp", {}) or {}
            status_msg = base_resp.get("status_msg", "API 返回空响应")
            _log.warning(f"[AI] API 返回空 choices: {status_msg}")
            return f"AI 服务暂时不可用: {status_msg}"

        content = response.choices[0].message.content

        # 清理响应内容
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
        elif content.startswith("```"):
            content = content[3:]
            if content.endswith("```"):
                content = content[:-3]

        return content.strip()
    except Exception as e:
        _log.error(f"AI response error: {e}", exc_info=True)
        return f"AI 服务出错: {str(e)}"


def stream_ai_response(self, messages: List[Dict], session_id: str, callback):
    """流式获取 AI 回复，通过回调逐段发送内容

    Args:
        messages: 消息列表
        session_id: 会话 ID
        callback: 回调函数，接收 (chunk: str) 参数
    """
    if not self.ai_client:
        _log.warning("AI client not initialized")
        callback("AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。")
        return

    try:
        # 获取流式响应
        for chunk in self.ai_client.chat_completion(
            model=self.ai_model, messages=messages, stream=True
        ):
            # 清理内容
            chunk = chunk.strip()
            if chunk:
                callback(chunk)

    except Exception as e:
        _log.error(f"AI stream error: {e}", exc_info=True)
        callback(f"\n\nAI 服务出错: {str(e)}")


def stream_send_response(
    server, session_id: str, message: Dict, thinking_content: str = None
):
    """通过 WebSocket 发送流式响应

    Args:
        session_id: 会话 ID
        message: 消息对象（包含完整的 content）
        thinking_content: AI 思考内容
    """
    try:
        content = message.get("content", "")
        _log.info(
            f"[Stream] 开始流式发送, session={session_id[:8]}, content长度={len(content)}"
        )

        # 发送开始事件
        server.socketio.emit(
            "ai_stream_start",
            {
                "session_id": session_id,
                "message": message,
                "thinking_content": thinking_content,
            },
            room=session_id,
        )
        _log.info("[Stream] 已发送 ai_stream_start")

        # 清理并分割内容
        content = content.strip()

        # 发送内容片段（每10个字符一段）
        chunk_size = 10
        chunk_count = 0
        for i in range(0, len(content), chunk_size):
            chunk = content[i : i + chunk_size]
            server.socketio.emit(
                "ai_stream_chunk",
                {
                    "session_id": session_id,
                    "message_id": message["id"],
                    "chunk": chunk,
                    "is_end": False,
                },
                room=session_id,
            )
            chunk_count += 1

        _log.info(f"[Stream] 已发送 {chunk_count} 个 chunk")

        # 发送结束事件
        server.socketio.emit(
            "ai_stream_end",
            {"session_id": session_id, "message_id": message["id"], "is_end": True},
            room=session_id,
        )
        _log.info("[Stream] 流式发送完成")

    except Exception as e:
        _log.error(f"Stream send error: {e}", exc_info=True)
        # 降级为普通发送
        server.socketio.emit(
            "ai_response",
            {"session_id": session_id, "message": message},
            room=session_id,
        )


def get_ai_response_with_images(
    server, messages: List[Dict], image_urls: List[str], user_question: str = None
) -> str:
    """获取带图片的 AI 回复（多模态）"""
    try:
        # 获取图片理解模型配置（新架构）
        vision_config = get_vision_model_config()
        api_key = None
        base_url = ""
        model = "zai-org/GLM-4.6V"
        provider_type = "openai_compatible"
        system_prompt = "请详细描述这张图片的内容。"

        if vision_config and vision_config.get("api_key"):
            # 使用新架构的配置
            api_key = vision_config.get("api_key")
            base_url = vision_config.get("base_url", "")
            model = vision_config.get("model", "zai-org/GLM-4.6V")
            provider_type = vision_config.get("provider_type", "openai_compatible")
            system_prompt = vision_config.get("system_prompt", "请详细描述这张图片的内容。")
        else:
            # 回退到旧的配置方式
            if not server.ai_client:
                return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

            api_key = getattr(server.ai_client, "api_key", None)
            base_url = getattr(server.ai_client, "base_url", None)
            model = getattr(server.ai_client, "pic_model", None) or "zai-org/GLM-4.6V"
            provider_type = getattr(server.ai_client, "provider_type", "openai_compatible")
            system_prompt = "请详细描述这张图片的内容。"

            # 尝试从config.ini获取silicon_api_key
            if not api_key:
                try:
                    import configparser
                    config = configparser.ConfigParser()
                    config.read("config.ini", encoding="utf-8")
                    api_key = config.get("ApiKey", "silicon_api_key", fallback="") or config.get("ApiKey", "api_key", fallback="")
                    base_url = "https://api.siliconflow.cn/v1"
                    model = config.get("pic", "model", fallback="zai-org/GLM-4.6V")
                except Exception:
                    pass

        if not api_key:
            _log.warning("API key not configured for image processing")
            return "图片处理服务未配置 API Key，请在 AI 配置中心配置图片理解模型。"

        # 构建多模态消息 - 简化版本，只包含当前图片，不包含历史记录
        multimodal_messages = []

        # 添加系统提示
        multimodal_messages.append({
            "role": "system",
            "content": "你是一个专业的图片分析助手。请详细描述图片中的内容，包括场景、人物、物体、颜色、氛围等细节。如果用户有具体问题，请结合图片内容回答。"
        })

        # 构建用户内容（图片 + 文本）
        user_content = []
        for img_url in image_urls:
            user_content.append(
                {"type": "image_url", "image_url": {"url": img_url}}
            )

        # 添加用户的原始问题或默认提示
        if user_question:
            user_text = user_question
        else:
            user_text = system_prompt
        user_content.append({"type": "text", "text": user_text})

        multimodal_messages.append({"role": "user", "content": user_content})

        # 调用多模态模型
        import requests

        # 构建请求URL
        if provider_type == "siliconflow" or "siliconflow" in base_url:
            url = "https://api.siliconflow.cn/v1/chat/completions"
        else:
            url = resolve_chat_completion_url(base_url, model=model, provider_type=provider_type)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": multimodal_messages,
            "stream": False,
        }

        response = requests.post(url, json=payload, headers=headers, timeout=120)
        response.raise_for_status()
        data = response.json()

        if not data.get("choices"):
            return "图片处理返回结果为空。"

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return content.strip() if content else "图片处理完成，但未返回内容。"

    except ImportError:
        return "图片处理失败：缺少 requests 库。"
    except Exception as e:
        # 回退到普通响应
        if user_question:
            temp_messages = messages.copy()
            temp_messages.append({"role": "user", "content": user_question})
            return server._get_ai_response(temp_messages)
        return f"处理图片时出错: {str(e)}"


def get_ai_response_with_tools(
server,
    messages: List[Dict],
    tools: List[Dict],
    use_silicon: bool = False,
    stop_event=None,
) -> Dict:
    """调用 AI 并支持工具

    Args:
        messages: 消息列表
        tools: 工具定义列表
        use_silicon: 是否使用 Silicon API（默认 False，使用主 API）
        stop_event: 可选的停止事件，用于立即停止
    """
    # 如果传入了 stop_event 且已设置，立即返回
    if stop_event and stop_event.is_set():
        raise StopIteration("用户停止生成")

    def check_stop():
        if stop_event and stop_event.is_set():
            raise StopIteration("用户停止生成")

    try:
        if not server.ai_client:
            return {"content": "AI 服务未配置"}

        import requests

        # 从设置中获取超时时间，默认 120 秒
        timeout = server.settings.get("api_timeout", 120)
        max_retries = server.settings.get("api_retry_count", 3)

        # 使用适当的超时：有工具时至少 60 秒，无工具时至少 30 秒
        # 这样可以更频繁地检查停止事件，同时不会因为太短而频繁超时
        if tools:
            api_timeout = max(timeout, 60)  # 有工具调用时至少 60 秒
        else:
            api_timeout = max(timeout, 30)  # 无工具调用时至少 30 秒

        # 检查是否应该使用 Silicon API
        # 只有在明确指定 use_silicon=True 且有 Silicon API key 时才使用
        if use_silicon:
            silicon_api_key = getattr(server.ai_client, "silicon_api_key", None)
            if not silicon_api_key:
                try:
                    import configparser

                    config = configparser.ConfigParser()
                    config.read("config.ini", encoding="utf-8")
                    silicon_api_key = config.get(
                        "ApiKey", "silicon_api_key", fallback=""
                    )
                except:
                    silicon_api_key = ""

            if not silicon_api_key:
                _log.info("[AI] Silicon API key 未配置，使用主 API")
                use_silicon = False

        if use_silicon:
            # Silicon API 调用
            url = "https://api.siliconflow.cn/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {silicon_api_key}",
                "Content-Type": "application/json",
            }
            # Silicon 支持的工具调用模型
            model = "Qwen/Qwen2.5-72B-Instruct"

            # 检查消息总长度，必要时截断工具结果
            MAX_CONTENT_LENGTH = 12000  # 每个消息内容的最大长度
            processed_messages = []
            for msg in messages:
                msg_copy = msg.copy()
                if "content" in msg_copy and isinstance(msg_copy["content"], str):
                    content_len = len(msg_copy["content"])
                    if content_len > MAX_CONTENT_LENGTH:
                        # 尝试保留 JSON 的完整性
                        truncated = msg_copy["content"][:MAX_CONTENT_LENGTH]
                        # 如果看起来像 JSON，尝试找到最后一个完整的括号
                        if truncated.strip().startswith(
                            "{"
                        ) or truncated.strip().startswith("["):
                            # 找到最后一个完整的 JSON 对象/数组
                            last_brace = max(
                                truncated.rfind("}"), truncated.rfind("]")
                            )
                            if (
                                last_brace > MAX_CONTENT_LENGTH - 500
                            ):  # 如果最后一个括号位置还合理
                                truncated = truncated[: last_brace + 1]
                        msg_copy["content"] = (
                            truncated
                            + f"\n... [内容过长，已截断，原始长度: {content_len} 字符]"
                        )
                processed_messages.append(msg_copy)

            payload = {
                "model": model,
                "messages": processed_messages,
                "tools": tools,
                "tool_choice": "auto",
            }

            # 重试机制
            last_error = None
            for attempt in range(max_retries):
                check_stop()  # 检查是否停止
                try:
                    _log.info(
                        f"[AI] Silicon API 调用 (尝试 {attempt + 1}/{max_retries})"
                    )

                    # 使用线程来执行请求，以便能够响应停止事件
                    import threading

                    result_container = {"data": None, "error": None}

                    def make_request():
                        try:
                            resp = requests.post(
                                url,
                                json=payload,
                                headers=headers,
                                timeout=api_timeout,
                            )
                            resp.raise_for_status()
                            result_container["data"] = resp.json()
                        except Exception as e:
                            result_container["error"] = e

                    request_thread = threading.Thread(target=make_request)
                    request_thread.daemon = True
                    request_thread.start()

                    # 等待请求完成，同时检查停止事件（每0.5秒检查一次）
                    while request_thread.is_alive():
                        check_stop()  # 如果停止事件被设置，这里会抛出 StopIteration
                        request_thread.join(timeout=0.5)

                    # 检查请求结果
                    if result_container["error"]:
                        raise result_container["error"]

                    data = result_container["data"]
                    if data is None:
                        raise Exception("请求未返回数据")

                    break
                except StopIteration:
                    # 用户停止生成，立即抛出
                    _log.info("[AI] 检测到停止信号，中断 Silicon API 请求")
                    raise
                except requests.exceptions.Timeout as e:
                    last_error = e
                    _log.warning(
                        f"[AI] Silicon API 超时 (尝试 {attempt + 1}/{max_retries}): {e}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(min(2**attempt, 2))  # 限制最大等待2秒
                    continue
                except requests.exceptions.RequestException as e:
                    last_error = e
                    _log.error(
                        f"[AI] Silicon API 错误 (尝试 {attempt + 1}/{max_retries}): {e}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(min(2**attempt, 2))
                    continue
            else:
                # 所有重试都失败
                raise last_error or Exception("API 调用失败")
        else:
            # 使用主 API
            url = resolve_chat_completion_url(
                server.ai_base_url,
                model=server.ai_model or "",
                provider_type=server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible")),
            )

            headers = {
                "Authorization": f"Bearer {server.ai_api_key}",
                "Content-Type": "application/json",
            }

            # 检查消息总长度，必要时截断工具结果
            MAX_CONTENT_LENGTH = 12000  # 每个消息内容的最大长度
            MAX_ARGUMENTS_LENGTH = 50000  # tool_calls arguments 的最大长度
            processed_messages = []
            for msg in messages:
                msg_copy = msg.copy()
                if "content" in msg_copy and isinstance(msg_copy["content"], str):
                    content_len = len(msg_copy["content"])
                    if content_len > MAX_CONTENT_LENGTH:
                        # 尝试保留 JSON 的完整性
                        truncated = msg_copy["content"][:MAX_CONTENT_LENGTH]
                        # 如果看起来像 JSON，尝试找到最后一个完整的括号
                        if truncated.strip().startswith(
                            "{"
                        ) or truncated.strip().startswith("["):
                            # 找到最后一个完整的 JSON 对象/数组
                            last_brace = max(
                                truncated.rfind("}"), truncated.rfind("]")
                            )
                            if (
                                last_brace > MAX_CONTENT_LENGTH - 500
                            ):  # 如果最后一个括号位置还合理
                                truncated = truncated[: last_brace + 1]
                        msg_copy["content"] = (
                            truncated
                            + f"\n... [内容过长，已截断，原始长度: {content_len} 字符]"
                        )

                # 检查 tool_calls arguments 长度
                if "tool_calls" in msg_copy and msg_copy["tool_calls"]:
                    for tc in msg_copy["tool_calls"]:
                        if "function" in tc and "arguments" in tc["function"]:
                            args_str = tc["function"]["arguments"]
                            if (
                                isinstance(args_str, str)
                                and len(args_str) > MAX_ARGUMENTS_LENGTH
                            ):
                                tc["function"]["arguments"] = (
                                    args_str[:MAX_ARGUMENTS_LENGTH]
                                    + f"\n... [参数过长已截断，原始长度: {len(args_str)}]"
                                )
                                _log.warning(
                                    f"[AI] 工具 {tc.get('function', {}).get('name')} 的 arguments 过长，已截断"
                                )

                processed_messages.append(msg_copy)

            payload = build_chat_completion_payload(
                server.ai_model,
                processed_messages,
                base_url=server.ai_base_url,
                provider_type=server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible")),
                tools=tools,
                tool_choice="auto",
            )

            # 记录 payload 大小
            import json

            payload_size = len(json.dumps(payload, ensure_ascii=False))
            _log.info(
                f"[AI] 发送请求到 {url}, 模型={server.ai_model}, 工具数={len(tools) if tools else 0}, payload大小={payload_size} bytes"
            )

            if payload_size > 500000:  # 超过 500KB
                _log.warning(
                    f"[AI] Payload 过大 ({payload_size} bytes)，可能导致 API 拒绝"
                )

            # 重试机制
            last_error = None
            for attempt in range(max_retries):
                check_stop()  # 检查是否停止
                try:
                    _log.info(
                        f"[AI] 主 API 调用 (尝试 {attempt + 1}/{max_retries})"
                    )

                    # 使用线程来执行请求，以便能够响应停止事件
                    import threading

                    result_container = {"data": None, "error": None}

                    def make_request():
                        try:
                            resp = requests.post(
                                url,
                                json=payload,
                                headers=headers,
                                timeout=api_timeout,
                            )
                            resp.raise_for_status()
                            result_container["data"] = resp.json()
                        except Exception as e:
                            result_container["error"] = e

                    request_thread = threading.Thread(target=make_request)
                    request_thread.daemon = True
                    request_thread.start()

                    # 等待请求完成，同时检查停止事件（每0.5秒检查一次）
                    while request_thread.is_alive():
                        check_stop()  # 如果停止事件被设置，这里会抛出 StopIteration
                        request_thread.join(timeout=0.5)

                    # 检查请求结果
                    if result_container["error"]:
                        raise result_container["error"]

                    data = result_container["data"]
                    if data is None:
                        raise Exception("请求未返回数据")

                    break
                except StopIteration:
                    # 用户停止生成，立即抛出
                    _log.info("[AI] 检测到停止信号，中断 API 请求")
                    raise
                except requests.exceptions.Timeout as e:
                    last_error = e
                    _log.warning(
                        f"[AI] 主 API 超时 (尝试 {attempt + 1}/{max_retries}): {e}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(min(2**attempt, 2))  # 限制最大等待2秒
                    continue
                except requests.exceptions.RequestException as e:
                    last_error = e
                    _log.error(
                        f"[AI] 主 API 错误 (尝试 {attempt + 1}/{max_retries}): {e}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(min(2**attempt, 2))
                    continue
            else:
                raise last_error or Exception("API 调用失败")

        normalized = normalize_chat_completion_data(
            data,
            base_url=server.ai_base_url or "",
            model=server.ai_model or "",
            provider_type=server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible")),
            fallback_tool_parser=server._parse_tool_call_from_text,
        )
        message = normalized.raw_message
        finish_reason = normalized.finish_reason

        _log.info(
            f"[AI] API 响应: finish_reason={finish_reason}, has_tool_calls={'tool_calls' in message}"
        )
        _log.debug(f"[AI] 工具数量: {len(tools) if tools else 0}")

        # 记录完整响应用于调试
        if "tool_calls" not in message and message.get("content", ""):
            _log.warning(
                f"[AI] 工具未生效，content 前100字符: {message.get('content', '')[:100]}"
            )

        result = normalized.to_dict()

        # 获取AI思考内容（如果API返回了的话）
        supports_reasoning = server.ai_config.get("supports_reasoning", True)
        thinking_content = normalized.thinking_content if supports_reasoning else ""
        if not supports_reasoning and "thinking_content" in result:
            result.pop("thinking_content", None)
        if thinking_content:
            _log.debug(f"[AI] 收到思考内容: {len(thinking_content)} 字符")
        elif normalized.thinking_content and not supports_reasoning:
            _log.info("[AI] 当前模型配置声明不展示 reasoning 字段，已忽略思考内容")
        if result.get("tool_calls") and "[TOOL_CALL]" in result.get("content", ""):
            cleaned = re.sub(
                r"\[TOOL_CALL\]\s*.*?\s*\[/TOOL_CALL\]\s*",
                "",
                result["content"],
                flags=re.DOTALL,
            )
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            result["content"] = cleaned.strip()
            _log.info(f"[AI] 成功解析 {len(result['tool_calls'])} 个工具调用")

        return result

    except StopIteration as e:
        # 用户停止生成
        _log.info(f"[AI] 停止生成: {e}")
        raise  # 重新抛出，让调用者处理

    except Exception as e:
        _log.error(f"AI with tools error: {e}")
        # 回退到普通 AI 调用
        content = server._get_ai_response(messages)
        return {"content": content}


def parse_tool_call_from_text(self, content: str) -> list:
    """解析 [TOOL_CALL] 格式的工具调用

    支持的格式：
    [TOOL_CALL]
    {tool => "exec_command", args => {
    --command ls -la
    --timeout 15
    }}
    [/TOOL_CALL]
    """
    import re

    tool_calls = []

    # 首先查找所有 [TOOL_CALL]...[/TOOL_CALL] 块
    # 使用简单的标记来分割
    pattern = r"\[TOOL_CALL\](.*?)\[/TOOL_CALL\]"
    matches = re.finditer(pattern, content, re.DOTALL)

    for match in matches:
        block = match.group(1).strip()

        # 提取工具名称
        name_match = re.search(r'tool\s*=>\s*["\']([^"\']+)["\']', block)
        if not name_match:
            # 尝试另一种格式: tool_name => "xxx" 或 "tool": "xxx"
            name_match = re.search(r'tool_name\s*=>\s*["\']([^"\']+)["\']', block)
            if not name_match:
                name_match = re.search(r'"tool":\s*"([^"]+)"', block)

        if not name_match:
            continue
        tool_name = name_match.group(1)

        # 提取参数块 { ... }
        # 找到 args => { 开始的位置
        args_start = block.find("args")
        if args_start == -1:
            continue

        # 找到第一个 {
        brace_start = block.find("{", args_start)
        if brace_start == -1:
            continue

        # 计算嵌套的括号，找到匹配的 }
        depth = 0
        args_end = brace_start
        for i in range(brace_start, len(block)):
            if block[i] == "{":
                depth += 1
            elif block[i] == "}":
                depth -= 1
                if depth == 0:
                    args_end = i
                    break

        args_block = block[brace_start + 1 : args_end]

        # 解析参数（--key value 格式）
        arguments = {}
        # 将参数按行分割
        lines = args_block.strip().split("\n")
        current_key = None
        current_value_lines = []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 检查是否是 --key 开头
            if line.startswith("--"):
                # 保存上一个参数
                if current_key is not None:
                    arguments[current_key] = "\n".join(current_value_lines).strip()

                # 解析新的 key
                parts = line[2:].split(
                    None, 1
                )  # 分割一次，空格前是key，后面是value
                current_key = parts[0] if parts else None
                current_value_lines = [parts[1]] if len(parts) > 1 else []
            elif current_key:
                # continuation of previous value
                current_value_lines.append(line)

        # 保存最后一个参数
        if current_key is not None:
            arguments[current_key] = "\n".join(current_value_lines).strip()

        tool_calls.append(
            {
                "id": f"tool_call_{len(tool_calls) + 1}",
                "name": tool_name,
                "arguments": arguments,
            }
        )

    return tool_calls
