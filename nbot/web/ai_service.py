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

try:
    from nbot.core.knowledge import get_knowledge_manager

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    KNOWLEDGE_MANAGER_AVAILABLE = False

_log = logging.getLogger(__name__)


def trigger_ai_response(
    server,
    session_id: str,
    user_content: str,
    sender: str,
    attachments=None,
    parent_message_id=None,
):
    """Trigger an AI response for a web session."""
    # 强制转换为列表
    if not attachments or not isinstance(attachments, list):
        attachments = []

    session = server.sessions.get(session_id)
    if not session:
        _log.warning(f"Session not found: {session_id}")
        server.log_message("warning", f"Session not found: {session_id}")
        return

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
    _log.info(f"[Knowledge] 知识库开关状态: {server.settings.get('knowledge', True)}")
    if server.settings.get("knowledge", True):
        try:
            _log.info(f"[Knowledge] 开始检索，查询: {user_content[:50]}...")
            knowledge_text = server._retrieve_knowledge(user_content)
            _log.info(f"[Knowledge] 检索结果长度: {len(knowledge_text)} 字符")
            if knowledge_text:
                knowledge_retrieved = True
                _log.info(f"[Knowledge] 知识库检索成功")
        except Exception as e:
            _log.error(f"[Knowledge] 知识库检索失败: {e}")

    def get_response():
        try:
            # 使用深拷贝避免修改原始消息
            import copy

            messages_for_ai = copy.deepcopy(session["messages"])

            # 检测"继续"意图
            tool_call_history = None
            if messages_for_ai and user_content.strip() in [
                "继续",
                "继续执行",
                "continue",
            ]:
                last_msg = messages_for_ai[-1]
                if last_msg.get("can_continue") and last_msg.get(
                    "tool_call_history"
                ):
                    tool_call_history = last_msg["tool_call_history"]
                    messages_for_ai.pop()  # 移除继续消息
                    messages_for_ai.pop()  # 移除不完整生成标记
                    _log.info(
                        f"[Continue] 检测到继续请求，恢复 {len(tool_call_history)} 条工具调用记录"
                    )

            # 限制历史长度
            MAX_HISTORY = 20
            MAX_TOTAL_CHARS = 30000  # 限制消息总字符数（大约）

            # 如果消息太多，只保留最近的
            if len(messages_for_ai) > MAX_HISTORY:
                messages_for_ai = [messages_for_ai[0]] + messages_for_ai[
                    -MAX_HISTORY:
                ]

            # 计算消息总字符数
            total_chars = sum(
                len(str(m.get("content", ""))) for m in messages_for_ai
            )
            if total_chars > MAX_TOTAL_CHARS:
                # 保留系统消息和最近的消息
                system_msg = (
                    messages_for_ai[0]
                    if messages_for_ai
                    and messages_for_ai[0].get("role") == "system"
                    else None
                )
                recent_msgs = (
                    messages_for_ai[-MAX_HISTORY:] if messages_for_ai else []
                )
                if system_msg:
                    messages_for_ai = [system_msg] + recent_msgs
                else:
                    messages_for_ai = recent_msgs
                _log.warning(
                    f"[Context] 消息过长，已压缩: 总字符数 {total_chars} -> {MAX_TOTAL_CHARS}"
                )

            # 添加知识库内容到系统消息
            if knowledge_text and messages_for_ai:
                if messages_for_ai[0].get("role") == "system":
                    messages_for_ai[0]["content"] += f"\n\n{knowledge_text}"
                else:
                    messages_for_ai.insert(
                        0, {"role": "system", "content": knowledge_text}
                    )

            # 检查附件并处理
            image_urls = []
            file_contents = []

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
            has_attachments = (
                attachments
                and isinstance(attachments, list)
                and len(attachments) > 0
            )

            if server.PROGRESS_CARD_AVAILABLE and server.progress_card_manager and server.socketio:
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
            if server.TODO_CARD_AVAILABLE and server.todo_card_manager and server.socketio:
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
                                elif att_path:
                                    # 尝试读取服务器上的图片文件
                                    try:
                                        import os

                                        file_path = os.path.join(
                                            server.static_folder,
                                            att_path.replace("/static/", ""),
                                        )
                                        if os.path.exists(file_path):
                                            with open(file_path, "rb") as f:
                                                import base64

                                                b64_data = base64.b64encode(
                                                    f.read()
                                                ).decode("utf-8")
                                                image_urls.append(
                                                    f"data:{att_type};base64,{b64_data}"
                                                )
                                                image_loaded = True
                                        else:
                                            _log.warning(
                                                f"图片文件不存在: {file_path}"
                                            )
                                    except Exception as e:
                                        _log.warning(
                                            f"读取图片文件失败: {att_name}, {e}"
                                        )

                                # 无论成功还是失败，都更新完成状态
                                if progress_card:
                                    _log.info(
                                        f"[ProgressCard] 准备更新 IMAGE_DONE，图片: {att_name}, 成功: {image_loaded}"
                                    )
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
                                    _log.info(f"[ProgressCard] IMAGE_DONE 更新完成")

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
                                        import os

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
                                        import os

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
                                    import os

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
                _log.info(f"[ProgressCard] 准备更新 KNOWLEDGE 步骤")
                progress_card.update(StepType.KNOWLEDGE, "📚 知识库检索...")
                progress_card.update(
                    StepType.KNOWLEDGE_DONE, "📚 知识库已加载", True
                )
                _log.info(f"[ProgressCard] KNOWLEDGE 步骤更新完成")

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

            # 检查是否有图片附件，如果有则使用多模态AI
            if image_urls:
                # 使用多模态AI处理图片，传递用户的原始问题
                assistant_content = server._get_ai_response_with_images(
                    messages_for_ai, image_urls, enhanced_content
                )
                final_content = assistant_content
                # 完成进度卡片
                complete_thinking_card()
            else:
                # 尝试使用工具调用（多轮）
                try:
                    from nbot.services.tools import get_enabled_tools, execute_tool

                    enabled_tools = get_enabled_tools()
                    # 记录加载的工具列表
                    tool_names = [
                        t.get("function", {}).get("name", "unknown")
                        for t in enabled_tools
                    ]
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

                        # 如果是继续请求，恢复工具调用历史
                        if tool_call_history:
                            tool_messages.extend(tool_call_history)
                            _log.info(
                                f"[Continue] 已恢复工具调用历史，当前消息数: {len(tool_messages)}"
                            )

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

                        stopped_prematurely = False
                        for iteration in range(max_iterations):
                            # 检查停止事件
                            if stop_event.is_set():
                                _log.info(
                                    f"[Stop] 检测到停止信号，终止生成: session_id={session_id}"
                                )
                                stopped_prematurely = True
                                break

                            # 更新进度卡片迭代计数
                            if progress_card:
                                progress_card.increment_iteration()

                            try:
                                response = server._get_ai_response_with_tools(
                                    tool_messages,
                                    enabled_tools,
                                    stop_event=stop_event,
                                )
                            except StopIteration:
                                # 用户停止生成
                                _log.info(f"[Stop] 检测到停止信号，终止生成")
                                stopped_prematurely = True
                                break

                            if "tool_calls" in response and response["tool_calls"]:
                                tool_calls = response["tool_calls"]
                                _log.info(
                                    f"[Tools] AI 调用工具: {[tc.get('name') for tc in tool_calls]}"
                                )

                                # 获取 AI 的思考内容（content），后续会关联到每个工具步骤
                                ai_thinking_content = response.get("content", "")

                                # 添加 AI 回复到消息历史
                                tool_messages.append(
                                    {
                                        "role": "assistant",
                                        "content": response.get("content", ""),
                                        "tool_calls": [
                                            {
                                                "id": tc.get(
                                                    "id", str(uuid.uuid4())
                                                ),
                                                "type": "function",
                                                "function": {
                                                    "name": tc["name"],
                                                    "arguments": json.dumps(
                                                        tc["arguments"]
                                                    ),
                                                },
                                            }
                                            for tc in tool_calls
                                        ],
                                    }
                                )

                                # 执行所有工具调用
                                for tool_call in tool_calls:
                                    tool_name = tool_call["name"]
                                    arguments = tool_call["arguments"]

                                    # 更新进度卡片 - 工具开始
                                    tool_display_name = {
                                        "search_news": "🔍 搜索新闻",
                                        "get_weather": "🌤️ 查询天气",
                                        "search_web": "🌐 网页搜索",
                                        "get_date_time": "🕐 获取时间",
                                        "http_get": "📡 获取网页",
                                        "understand_image": "🖼️ 理解图片",
                                        "workspace_create_file": "📝 创建文件",
                                        "workspace_read_file": "📖 读取文件",
                                        "workspace_edit_file": "✏️ 编辑文件",
                                        "workspace_delete_file": "🗑️ 删除文件",
                                        "workspace_list_files": "📁 列出文件",
                                        "workspace_tree": "🌳 显示目录树",
                                        "workspace_send_file": "📤 发送文件",
                                        "todo_add": "✅ 添加待办",
                                        "todo_list": "📋 列出待办",
                                        "todo_complete": "✓ 完成待办",
                                        "todo_delete": "🗑️ 删除待办",
                                        "todo_clear": "🧹 清空待办",
                                    }.get(tool_name, f"⚙️ {tool_name}")

                                    update_thinking_card(
                                        "tool",
                                        tool_display_name,
                                        json.dumps(arguments, ensure_ascii=False)[
                                            :100
                                        ],
                                        None,
                                        None,
                                        None,
                                        ai_thinking_content
                                        if ai_thinking_content
                                        else None,
                                    )

                                    # 工作区工具日志
                                    if tool_name.startswith("workspace_"):
                                        _log.info(
                                            f"[Workspace] 工具调用: {tool_name}"
                                        )
                                        args_str = json.dumps(
                                            arguments, ensure_ascii=False
                                        )
                                        if len(args_str) > 200:
                                            _log.info(
                                                f"[Workspace] 参数: {args_str[:200]}... (共 {len(args_str)} 字符)"
                                            )
                                        else:
                                            _log.info(
                                                f"[Workspace] 参数: {args_str}"
                                            )

                                    # 执行工具，传递 context
                                    tool_result = execute_tool(
                                        tool_name, arguments, context=tool_context
                                    )

                                    # 更新进度卡片 - 工具完成（保存完整参数、返回值和思考内容）
                                    if tool_result.get("success"):
                                        result_preview = str(
                                            tool_result.get(
                                                "content",
                                                tool_result.get(
                                                    "files", tool_result
                                                ),
                                            )
                                        )[:100]
                                        update_thinking_card(
                                            "tool_done",
                                            tool_display_name,
                                            result_preview,
                                            None,
                                            step_arguments=arguments,  # 保存完整参数
                                            step_full_result=tool_result,  # 保存完整返回值
                                            thinking_content=ai_thinking_content
                                            if ai_thinking_content
                                            else None,  # 保存AI思考内容
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
                                            else None,  # 保存AI思考内容
                                        )

                                    # 工作区工具日志
                                    if tool_name.startswith("workspace_"):
                                        if tool_result.get("success"):
                                            _log.info(
                                                f"[Workspace] ✓ 执行成功: {tool_name}"
                                            )
                                            _log.info(
                                                f"[Workspace] 结果预览: {str(tool_result)[:300]}"
                                            )
                                        else:
                                            _log.error(
                                                f"[Workspace] ✗ 执行失败: {tool_name} - {tool_result.get('error')}"
                                            )

                                    # Todo 工具日志
                                    if tool_name.startswith("todo_"):
                                        if tool_result.get("success"):
                                            _log.info(
                                                f"[Todo] ✓ 执行成功: {tool_name} - {tool_result.get('message', '')}"
                                            )
                                            # 更新 Todo 卡片
                                            if todo_card and server.TODO_CARD_AVAILABLE:
                                                try:
                                                    if tool_name == "todo_add":
                                                        todo_info = tool_result.get(
                                                            "todo", {}
                                                        )
                                                        todo_card.add_todo(
                                                            todo_id=todo_info.get(
                                                                "id"
                                                            ),
                                                            content=todo_info.get(
                                                                "content", ""
                                                            ),
                                                            priority=todo_info.get(
                                                                "priority", "medium"
                                                            ),
                                                        )
                                                    elif (
                                                        tool_name == "todo_complete"
                                                    ):
                                                        todo_info = tool_result.get(
                                                            "todo", {}
                                                        )
                                                        todo_card.complete_todo(
                                                            todo_info.get("id")
                                                        )
                                                    elif tool_name == "todo_delete":
                                                        deleted_todo = (
                                                            tool_result.get(
                                                                "deleted_todo", {}
                                                            )
                                                        )
                                                        todo_card.delete_todo(
                                                            deleted_todo.get("id")
                                                        )
                                                    elif tool_name == "todo_list":
                                                        todos = tool_result.get(
                                                            "todos", []
                                                        )
                                                        todo_card.update_todos(
                                                            todos
                                                        )
                                                    elif tool_name == "todo_clear":
                                                        todo_card.todos = []
                                                        todo_card._emit_update()
                                                except Exception as e:
                                                    _log.warning(
                                                        f"[TodoCard] 更新 Todo 卡片失败: {e}"
                                                    )
                                        else:
                                            _log.error(
                                                f"[Todo] ✗ 执行失败: {tool_name} - {tool_result.get('error', '')}"
                                            )

                                    # 处理 send_message 工具（不中断思考流程）
                                    if (
                                        tool_name == "send_message"
                                        and tool_result.get("action")
                                        == "send_message"
                                    ):
                                        # 立即发送消息给用户，不添加到 tool_messages
                                        progress_msg = {
                                            "id": str(uuid.uuid4()),
                                            "role": "assistant",
                                            "content": tool_result.get(
                                                "content", ""
                                            ),
                                            "timestamp": datetime.now().isoformat(),
                                            "sender": "AI",
                                            "message_type": tool_result.get(
                                                "message_type", "progress"
                                            ),
                                            "is_progress_message": True,  # 标记为进度消息
                                        }
                                        # 发送消息但不保存到 session（避免污染对话历史）
                                        server.socketio.emit(
                                            "progress_message",
                                            {
                                                "session_id": session_id,
                                                "message": progress_msg,
                                            },
                                            room=session_id,
                                        )
                                        _log.info(
                                            f"[SendMessage] 已发送进度消息: {tool_result.get('content', '')[:50]}..."
                                        )

                                        # 添加工具结果到消息历史（让 AI 知道消息已发送）
                                        tool_messages.append(
                                            {
                                                "role": "tool",
                                                "tool_call_id": tool_call.get(
                                                    "id", ""
                                                ),
                                                "content": json.dumps(
                                                    {
                                                        "success": True,
                                                        "message": "消息已发送",
                                                    },
                                                    ensure_ascii=False,
                                                ),
                                            }
                                        )
                                    else:
                                        # 普通工具，正常处理
                                        tool_messages.append(
                                            {
                                                "role": "tool",
                                                "tool_call_id": tool_call.get(
                                                    "id", ""
                                                ),
                                                "content": json.dumps(
                                                    tool_result, ensure_ascii=False
                                                ),
                                            }
                                        )

                                    # 如果是 workspace_send_file ，自动发送文件到 Web 端
                                    if (
                                        tool_name == "workspace_send_file"
                                        and tool_result.get("action") == "send_file"
                                    ):
                                        file_path = tool_result.get("path", "")
                                        filename = tool_result.get("filename", "")
                                        _log.info(
                                            f"[SendFile] 准备发送文件: {filename}, path: {file_path}, session_id: {session_id}"
                                        )

                                        if file_path and filename and session_id:
                                            try:
                                                # 直接构建文件消息并发送
                                                import os
                                                import mimetypes
                                                import shutil

                                                # 获取文件信息
                                                if not os.path.exists(file_path):
                                                    _log.error(
                                                        f"[SendFile] 文件不存在: {file_path}"
                                                    )
                                                else:
                                                    file_size = os.path.getsize(
                                                        file_path
                                                    )
                                                    mime_type, _ = (
                                                        mimetypes.guess_type(
                                                            file_path
                                                        )
                                                    )
                                                    if not mime_type:
                                                        mime_type = "application/octet-stream"
                                                    ext = os.path.splitext(
                                                        file_path
                                                    )[1].lower()
                                                    is_image = (
                                                        mime_type
                                                        and mime_type.startswith(
                                                            "image/"
                                                        )
                                                    )

                                                    # 复制文件到静态目录
                                                    files_dir = os.path.join(
                                                        server.static_folder, "files"
                                                    )
                                                    os.makedirs(
                                                        files_dir, exist_ok=True
                                                    )

                                                    import hashlib
                                                    import time

                                                    file_hash = hashlib.md5(
                                                        f"{file_path}{time.time()}".encode()
                                                    ).hexdigest()[:8]
                                                    safe_name = (
                                                        f"{file_hash}_{filename}"
                                                    )
                                                    dest_path = os.path.join(
                                                        files_dir, safe_name
                                                    )

                                                    # 确保目标目录存在（处理子目录情况）
                                                    os.makedirs(
                                                        os.path.dirname(dest_path),
                                                        exist_ok=True,
                                                    )

                                                    shutil.copy2(
                                                        file_path, dest_path
                                                    )
                                                    download_url = (
                                                        f"/static/files/{safe_name}"
                                                    )

                                                    # 构建文件消息
                                                    file_info = {
                                                        "id": str(uuid.uuid4()),
                                                        "role": "assistant",
                                                        "content": f"[文件: {filename}]",
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

                                                    # 对于图片，嵌入 base64 数据
                                                    if (
                                                        is_image
                                                        and file_size
                                                        < 5 * 1024 * 1024
                                                    ):
                                                        try:
                                                            import base64

                                                            with open(
                                                                file_path, "rb"
                                                            ) as f:
                                                                file_data = f.read()
                                                            b64_data = (
                                                                base64.b64encode(
                                                                    file_data
                                                                ).decode("utf-8")
                                                            )
                                                            file_info["file"][
                                                                "data"
                                                            ] = f"data:{mime_type};base64,{b64_data}"
                                                            file_info["file"][
                                                                "preview_url"
                                                            ] = file_info["file"][
                                                                "data"
                                                            ]
                                                        except Exception as img_err:
                                                            _log.warning(
                                                                f"[SendFile] 图片转base64失败: {img_err}"
                                                            )

                                                    # 保存到 session
                                                    if session_id in server.sessions:
                                                        server.sessions[session_id][
                                                            "messages"
                                                        ].append(file_info)
                                                        server._save_data("sessions")

                                                    # 发送文件消息到前端
                                                    server.socketio.emit(
                                                        "new_message",
                                                        file_info,
                                                        room=session_id,
                                                    )
                                                    _log.info(
                                                        f"[SendFile] 文件已发送: {filename} ({mime_type}, {file_size} bytes)"
                                                    )

                                            except Exception as send_err:
                                                _log.error(
                                                    f"[SendFile] 发送文件时出错: {send_err}",
                                                    exc_info=True,
                                                )
                                        else:
                                            _log.warning(
                                                f"[SendFile] 无法发送文件: file_path={file_path}, filename={filename}, session_id={session_id}"
                                            )
                            else:
                                # AI 没有调用工具，得到回复内容
                                final_content = response.get("content", "")
                                finish_reason = response.get("finish_reason", "")

                                # 检查是否为空回复（API 失败）
                                if not final_content:
                                    consecutive_errors += 1
                                    _log.warning(
                                        f"[AgentLoop] API 返回空内容，错误计数: {consecutive_errors}/{max_consecutive_errors}, iteration={iteration}"
                                    )
                                else:
                                    consecutive_errors = 0

                                # 判断是否应该终止思考
                                # 1. finish_reason == 'stop' 表示模型正常完成（OpenAI标准）
                                # 2. finish_reason 为空但 AI 返回了内容（某些国内API不返回finish_reason）
                                # 3. 回复以 'break' 结尾表示AI主动要求终止
                                # 4. 达到最大迭代次数
                                # 5. 连续错误次数超过阈值
                                should_stop = (
                                    finish_reason == "stop"
                                    or (
                                        not finish_reason and final_content
                                    )  # 兼容不返回finish_reason的API
                                    or final_content.rstrip().endswith("break")
                                    or iteration >= max_iterations - 1
                                    or consecutive_errors >= max_consecutive_errors
                                )

                                if should_stop:
                                    # 移除末尾的break标记（如果有）
                                    if final_content.rstrip().endswith("break"):
                                        final_content = final_content.rstrip()[
                                            :-5
                                        ].rstrip()
                                    _log.info(
                                        f"[AgentLoop] 终止思考: finish_reason={finish_reason}, iteration={iteration}"
                                    )
                                    break
                                else:
                                    # AI 没有要求终止，继续下一轮思考
                                    # 将AI的回复添加到消息历史
                                    tool_messages.append(
                                        {
                                            "role": "assistant",
                                            "content": final_content,
                                        }
                                    )
                                    _log.info(
                                        f"[AgentLoop] 继续思考: finish_reason={finish_reason}, iteration={iteration}"
                                    )
                                    # 继续下一轮迭代，给AI机会调用工具
                                    continue

                        if not final_content:
                            _log.warning(f"[Tools] AI 未生成最终回复，使用默认提示")
                            final_content = (
                                "抱歉，处理过程中出现了问题，请稍后再试~"
                            )

                        _log.info(f"[Tools] 最终回复长度: {len(final_content)}")

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
                tool_call_history = [
                    m
                    for m in tool_messages
                    if m.get("role") in ("assistant", "tool")
                ]
                _log.info(
                    f"[Stop] 保存工具调用历史，共 {len(tool_call_history)} 条记录"
                )

                assistant_message = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": "【生成已停止 - 工具调用记录已保存，回复「继续」可继续执行】",
                    "timestamp": datetime.now().isoformat(),
                    "sender": "AI",
                    "can_continue": True,
                    "tool_call_history": tool_call_history,
                }
                session["messages"].append(assistant_message)
                server._save_data("sessions")
                complete_thinking_card()
                server._stream_send_response(session_id, assistant_message)
                return

            assistant_content = final_content

            assistant_message = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": assistant_content,
                "timestamp": datetime.now().isoformat(),
                "sender": "AI",
            }

            # 使用流式发送
            _log.info(f"[Stream] _trigger_ai_response 准备发送流式响应")
            server._stream_send_response(session_id, assistant_message)

            session["messages"].append(assistant_message)

            # 更新 Token 统计
            estimated_tokens = len(user_content) + len(assistant_content)
            input_tokens = len(user_content)
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
            server._save_data("sessions")
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
                    _log.info(f"[SessionRename] 条件满足，开始生成新名称...")
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
                        _log.warning(f"[SessionRename] 生成名称失败，返回 None")
                else:
                    _log.info(
                        f"[SessionRename] 条件不满足: startswith={current_name.startswith('会话')}, empty={not current_name}, count={message_count}"
                    )
            except Exception as e:
                _log.error(f"[SessionRename] 自动重命名失败: {e}", exc_info=True)

        except Exception as e:
            _log.error(f"Error in AI response: {e}")
            error_message = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": f"抱歉，处理消息时出错: {str(e)}",
                "timestamp": datetime.now().isoformat(),
                "sender": "AI",
                "error": True,
            }
            session["messages"].append(error_message)
            server.socketio.emit(
                "ai_response",
                {"session_id": session_id, "message": error_message},
                room=session_id,
            )
            # 即使出错也保存会话
            server._save_data("sessions")
        finally:
            # 清理停止事件
            if session_id in server.stop_events:
                del server.stop_events[session_id]
                _log.info(f"[Stop] 清理停止事件: {session_id}")

    # 使用 Flask-SocketIO 的后台任务机制
    server.socketio.start_background_task(get_response)


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
    if not server.ai_client:
        _log.warning("AI client not initialized")
        callback("AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。")
        return

    try:
        # 获取流式响应
        for chunk in server.ai_client.chat_completion(
            model=server.ai_model, messages=messages, stream=True
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
        _log.info(f"[Stream] 已发送 ai_stream_start")

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
        _log.info(f"[Stream] 流式发送完成")

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
    if not self.ai_client:
        _log.warning("AI client not initialized")
        return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

    try:
        # 获取图片模型
        pic_model = getattr(self.ai_client, "pic_model", None)
        if not pic_model:
            try:
                import configparser

                config = configparser.ConfigParser()
                config.read("config.ini", encoding="utf-8")
                pic_model = config.get("pic", "model", fallback="glm-4v-flash")
            except:
                pic_model = "glm-4v-flash"

        # 获取 silicon API key
        silicon_api_key = getattr(self.ai_client, "silicon_api_key", None)
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
            _log.warning("Silicon API key not configured")
            return "图片处理服务未配置 Silicon API Key。"

        # 构建多模态消息
        multimodal_messages = []

        # 添加系统提示
        if messages and messages[0].get("role") == "system":
            multimodal_messages.append(messages[0])

        # 处理历史消息，只保留文本内容
        for msg in messages[1:]:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            multimodal_messages.append({"role": role, "content": content})

        # 构建用户内容（图片 + 文本）
        user_content = []
        for img_url in image_urls:
            user_content.append(
                {"type": "image_url", "image_url": {"url": img_url}}
            )

        # 添加用户的原始问题
        if user_question:
            user_text = user_question
        else:
            user_text = "请描述这些图片的内容并回答我的问题。"
        user_content.append({"type": "text", "text": user_text})

        multimodal_messages.append({"role": "user", "content": user_content})

        # 使用 Silicon API 调用多模态模型
        import requests

        url = "https://api.siliconflow.cn/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {silicon_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": pic_model,
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
        _log.error("requests library not available")
        return "图片处理失败：缺少 requests 库。"
    except Exception as e:
        _log.error(f"AI multimodal response error: {e}", exc_info=True)
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
                    _log.info(f"[AI] 检测到停止信号，中断 Silicon API 请求")
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
            url_base = (server.ai_base_url or "").rstrip("/")

            # 检查 base_url 是否已经是完整的 API 端点
            # MiniMax 的 base_url 通常已经包含完整的路径（如 /v1/text/chatcompletion_v2）
            if "completion" in url_base.lower() or url_base.endswith("/v1"):
                # 已经是完整端点，直接使用
                url = url_base
            else:
                # 需要添加 /chat/completions
                url = f"{url_base}/chat/completions"

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

            payload = {
                "model": server.ai_model,
                "messages": processed_messages,
                "tools": tools,
                "tool_choice": "auto",
            }

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
                    _log.info(f"[AI] 检测到停止信号，中断 API 请求")
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

        # 检查 choices 是否有效
        choices = data.get("choices")
        if not choices or len(choices) == 0:
            base_resp = data.get("base_resp", {})
            status_msg = base_resp.get("status_msg", "API 返回空响应")
            _log.warning(f"[AI] API 返回空 choices: {status_msg}")
            raise Exception(f"API 错误: {status_msg}")

        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "")

        _log.info(
            f"[AI] API 响应: finish_reason={finish_reason}, has_tool_calls={'tool_calls' in message}"
        )
        _log.debug(f"[AI] 工具数量: {len(tools) if tools else 0}")

        # 记录完整响应用于调试
        if "tool_calls" not in message and message.get("content", ""):
            _log.warning(
                f"[AI] 工具未生效，content 前100字符: {message.get('content', '')[:100]}"
            )

        result = {
            "content": message.get("content", ""),
            "finish_reason": finish_reason,
        }

        # 获取AI思考内容（如果API返回了的话）
        thinking_content = message.get("thinking_content", "")
        if thinking_content:
            result["thinking_content"] = thinking_content
            _log.debug(f"[AI] 收到思考内容: {len(thinking_content)} 字符")

        # 处理工具调用
        if "tool_calls" in message or finish_reason == "tool_calls":
            tool_calls = message["tool_calls"]
            result["tool_calls"] = []

            for tool_call in tool_calls:
                function_name = tool_call.get("function", {}).get("name")
                arguments = json.loads(
                    tool_call.get("function", {}).get("arguments", "{}")
                )

                result["tool_calls"].append(
                    {
                        "id": tool_call.get("id", ""),
                        "name": function_name,
                        "arguments": arguments,
                    }
                )
        else:
            # 检查 content 中是否包含 [TOOL_CALL] 格式（备用工具调用格式）
            content = result.get("content", "")
            if "[TOOL_CALL]" in content and "[/TOOL_CALL]" in content:
                _log.info(
                    f"[AI] 检测到 [TOOL_CALL] 格式，content 前200字符: {content[:200]}..."
                )
                parsed_calls = server._parse_tool_call_from_text(content)
                _log.info(
                    f"[AI] 解析结果: {len(parsed_calls)} 个工具调用, 内容: {parsed_calls}"
                )
                if parsed_calls:
                    result["tool_calls"] = parsed_calls
                    # 移除原始的 [TOOL_CALL] 内容
                    import re

                    cleaned = re.sub(
                        r"\[TOOL_CALL\]\s*.*?\s*\[/TOOL_CALL\]\s*",
                        "",
                        content,
                        flags=re.DOTALL,
                    )
                    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
                    result["content"] = cleaned.strip()
                    _log.info(f"[AI] 成功解析 {len(parsed_calls)} 个工具调用")

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
