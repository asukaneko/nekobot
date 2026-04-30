import os, json, datetime, time, re, copy
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


def _get_ai_response_with_tools_qq(messages: list, tools: list, session_id: str = None, max_iterations: int = 10) -> str:
    runtime_ai = refresh_runtime_ai_config()
    """
    QQ 端带工具调用的 AI 响应获取
    支持多轮工具调用，直到得到最终回复
    直接使用 HTTP 请求，不依赖 ai_client.chat_completion
    """
    if not TOOLS_AVAILABLE or not tools:
        response = ai_client.chat_completion(
            model=runtime_ai.get("model") or ai_client.model,
            messages=messages,
            stream=False,
        )
        return response.choices[0].message.content

    return _get_ai_response_with_tools_qq_unified(
        messages, tools, session_id=session_id, max_iterations=max_iterations
    )

    import requests
    
    if not TOOLS_AVAILABLE or not tools:
        # 不支持工具，直接调用 AI
        response = ai_client.chat_completion(
            model=runtime_ai.get("model") or ai_client.model,
            messages=messages,
            stream=False
        )
        return response.choices[0].message.content
    
    # 获取 API 配置
    url_base = (runtime_ai.get("base_url") or "").rstrip("/")
    if not url_base:
        raise ValueError("base_url 未配置")
    url = resolve_chat_completion_url(
        runtime_ai.get("base_url") or "",
        model=runtime_ai.get("model") or "",
        provider_type=runtime_ai.get("provider_type") or "openai_compatible",
    )
    
    headers = {
        "Authorization": f"Bearer {runtime_ai.get('api_key') or ''}",
        "Content-Type": "application/json"
    }
    
    tool_messages = copy.deepcopy(messages)
    final_content = ""
    
    for iteration in range(max_iterations):
        try:
            # 构造请求 payload
            payload = {
                "model": runtime_ai.get("model") or ai_client.model,
                "messages": tool_messages,
                "tools": tools,
                "tool_choice": "auto",
                "stream": False
            }
            
            # 发送请求
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            
            # 解析响应
            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            
            # 检查是否有工具调用
            if message.get("tool_calls"):
                tool_calls = message["tool_calls"]
                
                # 添加 AI 的回复到消息历史
                tool_messages.append({
                    "role": "assistant",
                    "content": message.get("content", ""),
                    "tool_calls": [
                        {
                            "id": tc.get("id"),
                            "type": "function",
                            "function": {
                                "name": tc.get("function", {}).get("name"),
                                "arguments": tc.get("function", {}).get("arguments")
                            }
                        } for tc in tool_calls
                    ]
                })
                
                # 执行所有工具调用
                for tool_call in tool_calls:
                    tool_name = tool_call.get("function", {}).get("name")
                    try:
                        arguments = json.loads(tool_call.get("function", {}).get("arguments", "{}"))
                    except:
                        arguments = {}
                    
                    print(f"[QQ Tools] 执行工具: {tool_name}, 参数: {arguments}")
                    
                    # 执行工具，传入 session_id（使用线程池避免阻塞）
                    try:
                        # 添加 session_id 到参数中
                        tool_context = {'session_id': session_id} if session_id else {}
                        
                        # 使用线程池异步执行工具，避免阻塞 Web 服务
                        future = _tool_executor.submit(execute_tool, tool_name, arguments, tool_context)
                        # 设置 60 秒超时
                        tool_result = future.result(timeout=60)
                        
                        # 检查是否需要用户确认（exec_command 的特殊处理）
                        if tool_result.get('require_confirmation'):
                            # 返回确认请求，中断工具调用流程
                            confirmation_msg = tool_result.get('message', f"AI 请求执行命令，需要您的确认。")
                            request_id = tool_result.get('request_id', '')
                            return f"{confirmation_msg}\n\n请回复「确认」来执行，或回复「取消」来拒绝。\n[请求ID: {request_id[:8] if request_id else 'N/A'}]"
                        
                        result_content = json.dumps(tool_result, ensure_ascii=False)
                    except TimeoutError:
                        result_content = json.dumps({"error": "工具执行超时（60秒）"}, ensure_ascii=False)
                    except Exception as e:
                        result_content = json.dumps({"error": str(e)}, ensure_ascii=False)
                    
                    # 添加工具结果到消息历史
                    tool_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.get("id"),
                        "content": result_content
                    })
                    
                    print(f"[QQ Tools] 工具结果: {result_content[:200]}...")
            else:
                # 没有工具调用，得到最终回复
                final_content = message.get("content", "")
                break
                
        except Exception as e:
            print(f"[QQ Tools] 工具调用出错: {e}")
            # 出错时返回最后一次 AI 回复或错误信息
            if tool_messages and tool_messages[-1].get("role") == "assistant":
                final_content = tool_messages[-1].get("content", "")
            if not final_content:
                final_content = f"处理出错: {str(e)}"
            break
    
    # 如果超过最大迭代次数，使用最后一条消息
    if not final_content and tool_messages:
        last_msg = tool_messages[-1]
        if last_msg.get("role") == "assistant":
            final_content = last_msg.get("content", "处理完成")
    
    return final_content


def _call_qq_ai_with_tools(messages: list, tools: list, stop_event=None) -> dict:
    import requests
    runtime_ai = refresh_runtime_ai_config()

    url_base = (runtime_ai.get("base_url") or "").rstrip("/")
    if not url_base:
        raise ValueError("base_url 未配置")
    url = resolve_chat_completion_url(
        runtime_ai.get("base_url") or "",
        model=runtime_ai.get("model") or "",
        provider_type=runtime_ai.get("provider_type") or "openai_compatible",
    )

    headers = {
        "Authorization": f"Bearer {runtime_ai.get('api_key') or ''}",
        "Content-Type": "application/json",
    }
    payload = build_chat_completion_payload(
        runtime_ai.get("model") or ai_client.model,
        messages,
        base_url=runtime_ai.get("base_url") or "",
        provider_type=runtime_ai.get("provider_type") or "openai_compatible",
        tools=tools,
        tool_choice="auto",
        stream=False,
    )

    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    normalized = normalize_chat_completion_data(
        data,
        base_url=runtime_ai.get("base_url") or "",
        model=runtime_ai.get("model") or "",
        provider_type=runtime_ai.get("provider_type") or "openai_compatible",
    )
    return normalized.to_dict()


def _get_ai_response_with_tools_qq_unified(
    messages: list, tools: list, session_id: str = None, max_iterations: int = 10
) -> str:
    def model_call(current_messages, stop_event=None):
        return _call_qq_ai_with_tools(current_messages, tools, stop_event=stop_event)

    def execute_qq_tool(tool_call, thinking_content, iteration, tool_messages):
        tool_name = tool_call.get("name")
        arguments = tool_call.get("arguments", {})
        print(f"[QQ Tools] 鎵ц宸ュ叿: {tool_name}, 鍙傛暟: {arguments}")

        try:
            tool_context = {"session_id": session_id} if session_id else {}
            future = _tool_executor.submit(
                execute_tool, tool_name, arguments, tool_context
            )
            tool_result = future.result(timeout=60)
            if tool_result.get("require_confirmation"):
                confirmation_msg = tool_result.get(
                    "message", "AI 请求执行命令，需要您的确认。"
                )
                request_id = tool_result.get('request_id', '')
                raise ToolLoopExit(
                    f"{confirmation_msg}\n\n请回复「确认」来执行，或回复「取消」来拒绝。\n[请求ID: {request_id[:8] if request_id else 'N/A'}]"
                )

            print(
                f"[QQ Tools] 宸ュ叿缁撴灉: {json.dumps(tool_result, ensure_ascii=False)[:200]}..."
            )
            return tool_result
        except TimeoutError:
            return {"error": "宸ュ叿鎵ц瓒呮椂锛?0绉掞級"}
        except ToolLoopExit:
            raise
        except Exception as e:
            return {"error": str(e)}

    try:
        execution_result = run_tool_loop_session(
            ToolLoopSession(
                initial_messages=messages,
                model_call=model_call,
                tool_executor=execute_qq_tool,
                max_iterations=max_iterations,
            )
        )
        loop_result = execution_result.loop_result
    except Exception as e:
        print(f"[QQ Tools] 宸ュ叿璋冪敤鍑洪敊: {e}")
        return f"澶勭悊鍑洪敊: {str(e)}"

    return resolve_loop_final_content(
        loop_result,
        "\u62b1\u6b49\uff0c\u6211\u6682\u65f6\u6ca1\u6709\u751f\u6210\u51fa\u6709\u6548\u56de\u590d\u3002",
    )

    if loop_result.final_content:
        return loop_result.final_content

    if loop_result.tool_messages:
        last_msg = loop_result.tool_messages[-1]
        if last_msg.get("role") == "assistant":
            return last_msg.get("content", "澶勭悊瀹屾垚")

    return "澶勭悊瀹屾垚"


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
         image: bool = False, url=None, video=None):
    adapter = get_channel_adapter("qq") or QQChannelAdapter()
    chat_request = adapter.build_chat_request(
        content=content,
        user_id=str(user_id) if user_id else None,
        attachments=[],
        metadata={
            "group_id": str(group_id) if group_id else None,
            "group_user_id": str(group_user_id) if group_user_id else None,
            "image": image,
            "url": url,
            "video": video,
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
    image = bool(chat_request.metadata.get("image", False))
    url = chat_request.metadata.get("url")
    video = chat_request.metadata.get("video")
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    qq_store = _get_qq_store()

    if user_id:
        user_id = str(user_id)
        history_messages = qq_store.ensure_history(user_id=user_id)
    elif group_id:
        group_id = str(group_id)
        history_messages = qq_store.ensure_history(
            group_id=group_id, group_user_id=group_user_id
        )
    else:
        if TOOLS_AVAILABLE and not runtime_ai.get("supports_tools", True):
            print("[QQ Tools] 当前配置声明不支持工具调用，已跳过工具链")
        if TOOLS_AVAILABLE and not runtime_ai.get("supports_tools", True):
            print("[QQ Tools] 当前配置声明不支持工具调用，已跳过工具链")
        history_messages = []

    messages = copy.deepcopy(history_messages)

    # === 确认/拒绝待执行命令检测（QQ 通道） ===
    _confirmation_handled = False
    if content and not image and not video and TOOLS_AVAILABLE and get_pending_by_session:
        try:
            session_id_check = get_qq_session_id(user_id, group_id, group_user_id)
            content_stripped = content.strip().lower()
            is_confirm = any(kw == content_stripped or (len(content_stripped) <= 4 and kw in content_stripped) for kw in _CONFIRM_KEYWORDS)
            is_reject = any(kw == content_stripped or (len(content_stripped) <= 4 and kw in content_stripped) for kw in _REJECT_KEYWORDS)

            if is_confirm and not is_reject:
                request_id = get_pending_by_session(session_id_check)
                if request_id:
                    print(f'[QQ Confirm] 用户确认执行待处理命令: session={session_id_check}, request_id={request_id[:8]}')
                    exec_result = execute_pending_command(request_id)
                    if exec_result.get('executed'):
                        cmd = exec_result.get('command', '')
                        stdout = exec_result.get('stdout', '')
                        stderr = exec_result.get('stderr', '')
                        result_msg = f"[系统] 用户已确认执行命令 `{cmd}`。\n\n执行结果:\n{stdout}"
                        if stderr:
                            result_msg += f"\n\n错误输出:\n{stderr}"
                        messages.append({"role": "user", "content": result_msg})
                        _confirmation_handled = True
                    else:
                        error_msg = exec_result.get('error', '命令执行失败')
                        messages.append({"role": "user", "content": f"[系统] 执行命令失败: {error_msg}"})
                        _confirmation_handled = True
            elif is_reject:
                request_id = get_pending_by_session(session_id_check)
                if request_id:
                    print(f'[QQ Reject] 用户拒绝执行待处理命令: session={session_id_check}')
                    reject_result = reject_pending_command(request_id)
                    cmd = reject_result.get('command', '')
                    messages.append({"role": "user", "content": f"[系统] 用户已拒绝执行命令 `{cmd}`。"})
                    _confirmation_handled = True
        except Exception as e:
            print(f"[QQ Confirm] failed to handle pending command confirmation: {e}")
    if group_user_id:
        pre_text = f"用户{group_user_id}说："
    else:
        pre_text = ""

    # 搜索功能已移除，使用工具调用替代
    search_status = 0
    search_res = ""

    if image:
        print(f"[图片识别] chat 函数收到图片请求, URL: {url}")
        response = chat_image(url)
        print(f"[图片识别] chat 函数获取到图片描述: {response[:80] if response else '空'}...")
        messages.append({"role": "user", "content": f"(当前时间：{now_time})"})
        if search_status == 1:
            messages.append({"role": "user", "content": f"{pre_text}用户发送了一张图片，这是图片的描述：{response} 这是联网搜索的结果：{search_res}这是用户说的话：{content}"})
        else:
            messages.append({"role": "user", "content": f"{pre_text}用户发送了一张图片，这是图片的描述：{response} 这是用户说的话：{content}"})
    elif video:
        response = chat_video(video)
        messages.append({"role": "user", "content": f"(当前时间：{now_time})"})
        messages.append({"role": "user", "content": f"{pre_text}这是视频的描述：{response}这是用户说的话：{content}"})
    else:
        messages.append({"role": "user", "content": f"(当前时间：{now_time})"})
        if search_status == 1:
            messages.append({"role": "user", "content": f"{pre_text}这是联网搜索的结果：{search_res}这是用户说的话：{content}"})
        else:
            messages.append({"role": "user", "content": f"{pre_text}{content}"})

    des = ""
    pattern = r"(?:https?:\/\/)?(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})+(?:\/[^\s?]*)?(?:\?[^\s]*)?"
    matches = re.findall(pattern, content)
    if matches:
        tot = 0
        for match in matches:
            tot += 1
            des += f"第{tot}个链接{match}的描述：" + chat_webpage(match) + "\n"
        messages.append({"role": "user", "content": f"{pre_text}{des}"})

    # 记录用户消息到新消息模块
    record_user_message(content, user_id, group_id, group_user_id)

    # 知识库检索 - 根据用户提问匹配相关内容
    knowledge_res = search_knowledge_base(content, user_id, group_id)
    prepared_context = prepare_chat_context(
        messages,
        content,
        knowledge_text=knowledge_res,
        max_total_chars=100000,
    )
    messages = prepared_context.messages

    # 获取工作区上下文（用于工具调用）
    workspace_context = get_workspace_context(user_id, group_id, group_user_id)
    
    # 使用带工具调用的 AI 响应获取
    if (
        TOOLS_AVAILABLE
        and runtime_ai.get("supports_tools", True)
        and channel_capabilities.supports_file_send
        and workspace_context.get('session_id')
    ):
        # 准备工具上下文
        tool_context = {
            'session_id': workspace_context['session_id'],
            'user_id': user_id,
            'group_id': group_id,
            'source': 'qq'
        }
        
        # 执行带工具的 AI 调用
        assistant_response = _get_ai_response_with_tools_qq(
            messages, 
            TOOL_DEFINITIONS,
            session_id=workspace_context.get('session_id'),
            max_iterations=10
        )
    else:
        # 普通 AI 调用（不支持工具）
        response = ai_client.chat_completion(
            model=runtime_ai.get("model") or ai_client.model,
            messages=messages,
            stream=False
        )
        assistant_response = response.choices[0].message.content

    if not assistant_response:
        print("[DEBUG] API返回内容为空")

    # 获取 token 使用量（工具调用模式下使用估算）
    prompt_tokens = len(str(messages))
    completion_tokens = len(assistant_response)
    total_tokens = prompt_tokens + completion_tokens

    # 更新 token 统计（使用真实数据）
    _update_token_stats(user_id, group_id, prompt_tokens, completion_tokens, total_tokens)

    # 注意：QQ 消息已通过新模块 message_manager 统一管理，存储在 data/qq/ 目录
    # 不再同步到 data/web/sessions.json

    assistant_response = clean_response_content(assistant_response)

    # 解析 JSON 返回给 QQ
    display_response = extract_display_text(assistant_response)
    if assistant_response and assistant_response.strip().startswith('{'):
        try:
            # 先替换中文引号和冒号为英文
            fixed = assistant_response.replace(chr(8220), '"').replace(chr(8221), '"').replace(chr(65306), ':')
            parsed = json.loads(fixed)
            if isinstance(parsed, dict) and 'msg' in parsed:
                display_response = parsed['msg']
        except:
            pass

    # 注意：AI回复的记录已通过 BotAPI 的补丁自动处理（wrapped_post_group_msg）
    # 这里不需要再调用 record_assistant_message，避免重复保存

    qq_store.save()

    chat_response = ChatResponse(final_content=display_response)
    chat_response.assistant_message = adapter.build_assistant_message(
        chat_response,
        conversation_id=chat_request.conversation_id,
        sender="AI",
    )
    return chat_response


def _update_token_stats(user_id, group_id, prompt_tokens, completion_tokens, total_tokens):
    """更新 Token 统计（使用真实数据）"""
    try:
        import os
        import json
        from datetime import datetime

        data_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'web')
        os.makedirs(data_dir, exist_ok=True)
        stats_file = os.path.join(data_dir, 'token_stats.json')

        # 加载现有统计
        stats = {}
        if os.path.exists(stats_file):
            try:
                with open(stats_file, 'r', encoding='utf-8') as f:
                    stats = json.load(f)
            except:
                stats = {}

        # 初始化默认值
        if 'today' not in stats:
            stats['today'] = 0
        if 'month' not in stats:
            stats['month'] = 0
        if 'history' not in stats:
            stats['history'] = []
        if 'sessions' not in stats:
            stats['sessions'] = {}
        if 'models' not in stats:
            stats['models'] = {}

        # 更新今日和本月统计
        stats['today'] += total_tokens
        stats['month'] += total_tokens

        # 更新历史记录（按天）
        today_str = datetime.now().strftime('%Y-%m-%d')
        today_entry = None
        for entry in stats['history']:
            if entry.get('date') == today_str:
                today_entry = entry
                break

        if today_entry:
            today_entry['input'] += prompt_tokens
            today_entry['output'] += completion_tokens
            today_entry['total'] += total_tokens
        else:
            stats['history'].append({
                'date': today_str,
                'input': prompt_tokens,
                'output': completion_tokens,
                'total': total_tokens
            })

        # 限制历史记录数量（保留最近30天）
        if len(stats['history']) > 30:
            stats['history'] = sorted(stats['history'], key=lambda x: x['date'])[-30:]

        # 更新会话统计
        session_id = str(user_id) if user_id else str(group_id)
        if session_id not in stats['sessions']:
            stats['sessions'][session_id] = {
                'input': 0,
                'output': 0,
                'total': 0,
                'type': 'private' if user_id else 'group',
                'message_count': 0
            }
        stats['sessions'][session_id]['input'] += prompt_tokens
        stats['sessions'][session_id]['output'] += completion_tokens
        stats['sessions'][session_id]['total'] += total_tokens
        stats['sessions'][session_id]['message_count'] = stats['sessions'][session_id].get('message_count', 0) + 2  # 用户消息 + AI回复

        # 保存统计
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

    except Exception as e:
        print(f"更新 Token 统计失败: {e}")


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
