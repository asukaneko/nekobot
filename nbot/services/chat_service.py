import os, json, datetime, time, re
from nbot.services.ai import (
    ai_client, user_messages, group_messages, MAX_HISTORY_LENGTH,
    model, api_key, base_url
)

last_log_entry = {}


def remove_brackets_content(text: str) -> str:
    text = re.sub(r'（.*?）', '', text)
    text = re.sub(r'【.*?】', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'\{.*?\}', '', text)
    text = re.sub(r'\「.*?\」', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text.strip()


def load_memories(user_id=None, group_id=None):
    """加载长期和短期记忆"""
    memories = []
    target_id = user_id or group_id
    
    if not target_id:
        return ""
    
    target_id = str(target_id)
    data_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'web')
    memory_file = os.path.join(data_dir, "memories.json")
    
    if not os.path.exists(memory_file):
        return ""
    
    try:
        with open(memory_file, 'r', encoding='utf-8') as f:
            all_memories = json.load(f)
    except:
        return ""
    
    now = datetime.datetime.now()
    
    for mem in all_memories:
        # 检查记忆是否关联到当前用户/群
        mem_target = mem.get('target_id', '')
        if mem_target and mem_target != target_id:
            continue
            
        mem_type = mem.get('type', 'long')
        
        if mem_type == 'long':
            # 长期记忆直接加入
            memories.append(f"[{mem.get('key', '')}]: {mem.get('value', '')}")
        elif mem_type == 'short':
            # 短期记忆检查是否过期
            created_at = mem.get('created_at', '')
            expire_days = mem.get('expire_days', 7)
            
            if created_at:
                try:
                    created = datetime.datetime.fromisoformat(created_at)
                    diff_days = (now - created).days
                    if diff_days <= expire_days:
                        memories.append(f"[{mem.get('key', '')}]: {mem.get('value', '')}")
                except:
                    memories.append(f"[{mem.get('key', '')}]: {mem.get('value', '')}")
    
    if memories:
        return "\n".join(["【重要记忆】"] + memories)
    return ""


def load_prompt(user_id=None, group_id=None, include_skills: bool = True):
    prompt_file = None
    prompt = ""

    if user_id:
        user_id = str(user_id)
        prompt_file = f"resources/prompts/user/user_{user_id}.txt"
    elif group_id:
        group_id = str(group_id)
        prompt_file = f"resources/prompts/group/group_{group_id}.txt"

    if prompt_file:
        try:
            with open(prompt_file, "r", encoding="utf-8") as file:
                prompt = file.read()
        except FileNotFoundError:
            pass

    if not prompt:
        try:
            with open("resources/prompts/neko.txt", "r", encoding="utf-8") as file:
                prompt = file.read()
        except FileNotFoundError:
            prompt = ""

    # 加载记忆并添加到提示词
    memories_text = load_memories(user_id, group_id)
    if memories_text:
        if prompt:
            prompt = prompt + "\n\n" + memories_text
        else:
            prompt = memories_text

    # 添加可用工具列表到提示词（从web配置读取启用的工具）
    try:
        from nbot.services.tools import get_enabled_tools
        enabled_tools = get_enabled_tools()
        if enabled_tools:
            tools_text = "## 可用工具 (Tools)\n"
            tools_text += "你可以使用以下工具来帮助用户：\n\n"
            for tool in enabled_tools:
                if tool.get("type") == "function" and "function" in tool:
                    func = tool["function"]
                    name = func.get("name", "")
                    desc = func.get("description", "")
                    if name and desc:
                        tools_text += f"- **{name}**: {desc}\n"
            tools_text += "\n**使用规则：**\n"
            tools_text += "1. 当用户请求需要使用工具时，你可以调用对应的工具\n"
            tools_text += "2. 工具调用会被系统自动处理\n"
            if tools_text:
                if prompt:
                    prompt = prompt + "\n\n" + tools_text
                else:
                    prompt = tools_text
    except Exception:
        pass

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
    return ai_client.describe_image(iurl, "请描述这个图片的内容，仅作描述，不要分析内容")


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


def judge_search(content: str) -> bool:
    return ai_client.should_search(content)


def judge_reply(content: str) -> float:
    return ai_client.should_reply(content)


def chat(content: str = "", user_id=None, group_id=None, group_user_id=None,
         image: bool = False, url=None, video=None):
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if user_id:
        user_id = str(user_id)
        prompt = load_prompt(user_id=user_id)

        if user_id not in user_messages:
            user_messages[user_id] = [{"role": "system", "content": prompt}]
        else:
            if user_messages[user_id] and user_messages[user_id][0].get("role") == "system":
                user_messages[user_id][0]["content"] = prompt
            else:
                user_messages[user_id].insert(0, {"role": "system", "content": prompt})
        messages = user_messages[user_id]
    elif group_id:
        group_id = str(group_id)
        prompt = load_prompt(group_id=group_id)

        # 群聊中每个用户有独立的会话
        if group_user_id:
            session_key = f"{group_id}_{group_user_id}"
        else:
            session_key = group_id

        if session_key not in group_messages:
            group_messages[session_key] = [{"role": "system", "content": prompt}]
        else:
            if group_messages[session_key] and group_messages[session_key][0].get("role") == "system":
                group_messages[session_key][0]["content"] = prompt
            else:
                group_messages[session_key].insert(0, {"role": "system", "content": prompt})
        messages = group_messages[session_key]
    else:
        messages = []

    if group_user_id:
        pre_text = f"用户{group_user_id}说："
    else:
        pre_text = ""

    if content.startswith("搜索") or ("搜索" in content) or judge_search(content):
        search_status = 1
        search_res = online_search(content)
    else:
        search_status = 0
        search_res = ""

    if image:
        response = chat_image(url)
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

    if len(messages) > MAX_HISTORY_LENGTH:
        messages = [messages[0]] + messages[-MAX_HISTORY_LENGTH:]

    response = ai_client.chat_completion(
        model=model,
        messages=messages,
        stream=False
    )
    assistant_response = response.choices[0].message.content

    if not assistant_response:
        print("[DEBUG] API返回内容为空")

    # 获取真实的 token 使用量
    usage = response.usage if hasattr(response, 'usage') else None
    if usage:
        prompt_tokens = usage.prompt_tokens
        completion_tokens = usage.completion_tokens
        total_tokens = usage.total_tokens
    else:
        # 如果没有 usage 信息，使用估算
        prompt_tokens = len(str(messages))
        completion_tokens = len(assistant_response)
        total_tokens = prompt_tokens + completion_tokens

    # 更新 token 统计（使用真实数据）
    _update_token_stats(user_id, group_id, prompt_tokens, completion_tokens, total_tokens)

    # 同步用户消息和 AI 回复到 Web 会话（群聊时每个用户独立会话）
    _sync_to_web_session('user', content, user_id, group_id, group_user_id)
    _sync_to_web_session('assistant', assistant_response, user_id, group_id, group_user_id)

    temp_content = assistant_response.strip()
    if temp_content.startswith("```json"):
        temp_content = temp_content[7:]
        if temp_content.endswith("```"):
            temp_content = temp_content[:-3]
        assistant_response = temp_content.strip()
    elif temp_content.startswith("```"):
        temp_content = temp_content[3:]
        if temp_content.endswith("```"):
            temp_content = temp_content[:-3]
        assistant_response = temp_content.strip()

    assistant_response = assistant_response.lstrip()

    # 解析 JSON 返回给 QQ
    display_response = assistant_response
    if assistant_response and assistant_response.strip().startswith('{'):
        try:
            parsed = json.loads(assistant_response)
            if isinstance(parsed, dict) and 'msg' in parsed:
                display_response = parsed['msg']
        except:
            # 尝试替换中文引号
            try:
                fixed = assistant_response.replace('"', '"').replace('"', '"')
                parsed = json.loads(fixed)
                if isinstance(parsed, dict) and 'msg' in parsed:
                    display_response = parsed['msg']
            except:
                pass

    try:
        with open("saved_message/user_messages.json", "w", encoding='utf-8') as f:
            json.dump(user_messages, f, ensure_ascii=False, indent=4)
        with open("saved_message/group_messages.json", "w", encoding='utf-8') as f:
            json.dump(group_messages, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存历史记录失败: {e}")

    return display_response


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
                'type': 'private' if user_id else 'group'
            }
        stats['sessions'][session_id]['input'] += prompt_tokens
        stats['sessions'][session_id]['output'] += completion_tokens
        stats['sessions'][session_id]['total'] += total_tokens

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
    sessions_file = os.path.join(data_dir, "sessions.json")
    
    # 加载现有会话
    sessions = {}
    if os.path.exists(sessions_file):
        try:
            with open(sessions_file, 'r', encoding='utf-8') as f:
                sessions = json.load(f)
        except:
            sessions = {}
    
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
        with open(sessions_file, 'w', encoding='utf-8') as f:
            json.dump(sessions, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] 已同步消息到 sessions.json, session_id: {session_id}, qq_id: {qq_id}")
    except Exception as e:
        print(f"同步到 Web 会话失败: {e}")


def _record_message(role, content, user_id=None, group_id=None):
    if not content:
        return

    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if role == "user" and "(当前时间：" not in content:
        record_content = f"(当前时间：{now_time})\n{content}"
    else:
        record_content = content

    if user_id:
        user_id = str(user_id)
        prompt = load_prompt(user_id=user_id)
        if user_id not in user_messages:
            user_messages[user_id] = [{"role": "system", "content": prompt}]
        else:
            if user_messages[user_id] and user_messages[user_id][0].get("role") == "system":
                user_messages[user_id][0]["content"] = prompt
            else:
                user_messages[user_id].insert(0, {"role": "system", "content": prompt})

        user_messages[user_id].append({"role": role, "content": record_content})
        if len(user_messages[user_id]) > MAX_HISTORY_LENGTH:
            user_messages[user_id] = [user_messages[user_id][0]] + user_messages[user_id][-MAX_HISTORY_LENGTH:]
    elif group_id:
        group_id = str(group_id)
        prompt = load_prompt(group_id=group_id)
        if group_id not in group_messages:
            group_messages[group_id] = [{"role": "system", "content": prompt}]
        else:
            if group_messages[group_id] and group_messages[group_id][0].get("role") == "system":
                group_messages[group_id][0]["content"] = prompt
            else:
                group_messages[group_id].insert(0, {"role": "system", "content": prompt})

        group_messages[group_id].append({"role": role, "content": record_content})
        if len(group_messages[group_id]) > MAX_HISTORY_LENGTH:
            group_messages[group_id] = [group_messages[group_id][0]] + group_messages[group_id][-MAX_HISTORY_LENGTH:]

    try:
        with open("saved_message/user_messages.json", "w", encoding="utf-8") as f:
            json.dump(user_messages, f, ensure_ascii=False, indent=4)
        with open("saved_message/group_messages.json", "w", encoding="utf-8") as f:
            json.dump(group_messages, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存历史记录失败: {e}")


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


def record_assistant_message(content, user_id=None, group_id=None):
    _record_message("assistant", content, user_id, group_id)


def record_user_message(content, user_id=None, group_id=None):
    _record_message("user", content, user_id, group_id)


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
        summary = ai_client.summarize_text(system_prompt, user_prompt, model=model)
        return summary or "总结结果为空喵~"
    except Exception:
        return "总结时出错喵，请稍后再试~"


def generate_today_summary(user_id=None, group_id=None) -> str:
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
            client = OpenAI(api_key=api_key, base_url=base_url)
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
                    model=model,
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
