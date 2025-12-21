# 提示词和聊天记录保存在项目目录下，与代码直接关联，不建议修改。保存的图片可以在配置文件中配置
#  .saved_message
#   |--- ...
#  .prompts
#   |---... 
import configparser,requests,os,base64,time,json,datetime,re
from openai import OpenAI
from ncatbot.core.element import Record,MessageChain

config_parser = configparser.ConfigParser()
config_parser.read('config.ini')

user_messages = {}
group_messages = {}

try:
    with open("saved_message/user_messages.json","r",encoding="utf-8") as f:
        user_messages = json.load(f)
    with open("saved_message/group_messages.json","r",encoding="utf-8") as f:
        group_messages = json.load(f) 
except FileNotFoundError:
    os.makedirs("saved_message",exist_ok = True)
    
api_key = config_parser.get('ApiKey', 'api_key')
base_url = config_parser.get('ApiKey', 'base_url')
model = config_parser.get('ApiKey', 'model')
MAX_HISTORY_LENGTH = config_parser.getint('chat', 'MAX_HISTORY_LENGTH')
#用于图片识别：
pic_model = config_parser.get('pic', 'model')
cache_address = config_parser.get('cache','cache_address')

voice = config_parser.get('voice','voice')

#用于在线搜索：
search_api_key = config_parser.get('search','api_key')
search_api_url = config_parser.get('search','api_url')

video_api = config_parser.get('video','api_key')

def remove_brackets_content(text) -> str:
    """
    删除字符串中所有括号及其内容，包括：
    - 中文括号：（）【】
    - 英文括号：()
    - 花括号：{}
    """
    text = re.sub(r'（.*?）', '', text) 
    text = re.sub(r'【.*?】', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'\{.*?\}', '', text)
    text = re.sub(r'\「.*?\」', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text.strip()

def load_prompt(user_id=None, group_id=None):
    """
    加载用户或群组的提示词。
    :param user_id: 用户ID。
    :param group_id: 群组ID。
    :return: 提示词字符串。
    """
    prompt_file = None
    if user_id:
        user_id = str(user_id)
        prompt_file = f"prompts/user/user_{user_id}.txt"
    elif group_id:
        group_id = str(group_id)
        prompt_file = f"prompts/group/group_{group_id}.txt"

    try:
        with open(prompt_file, "r", encoding="utf-8") as file:
            prompt = file.read()
    except FileNotFoundError:
        try:
            with open("neko.txt", "r", encoding="utf-8") as file:
                prompt = file.read()
        except FileNotFoundError:
            prompt = ""
    try:
        from commands import get_all_help_text_for_prompt
        help_text = get_all_help_text_for_prompt()
        if help_text:
            if prompt:
                return prompt + "\n\n" + help_text
            return help_text
    except Exception:
        return prompt
    return prompt

def online_search(content) -> str:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {search_api_key}"
    }
    data = {
        "query": content,
        "query_rewrite": True,
        "top_k":6
    }
    try:
        response = requests.post(search_api_url, headers=headers, json=data)
        return str(response.json()["result"]["search_result"])
    except:
        return ""

def chat_image(iurl) -> str:
    """
    图片识别。
    :param url: 图片URL。
    :return: 图片识别结果。
    """
    url = "https://api.siliconflow.cn/v1/chat/completions"

    payload = {
            "model":pic_model,
            "messages": [
            {
            "role": "user",
            "content": [
                {
                    "type": "image_url",
                    "image_url": {
                        "url": iurl
                    }
                },
                {
                    "type": "text", 
                    "text": "请描述这个图片的内容，仅作描述，不要分析内容"
                }
            ]
            }
            ]
    }
    headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
    }
    response = requests.post(url, json=payload, headers=headers)
    try:
        return response.json()["choices"][0]["message"]["content"]
    except:
        return "链接失效"

def chat_video(vurl) -> str:
    """
    视频识别。
    :param url: 视频URL。
    :return: 视频识别结果。
    """
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"

    payload = {
            "model": "glm-4.5v",
            "messages": [
            {
            "role": "user",
            "content": [
                {
                    "type": "video_url",
                    "video_url": {
                        "url": vurl
                    }
                },
                {
                    "type": "text", 
                    "text": "请分析这个视频的内容"
                }
            ]
            }
            ]
    }
    headers = {
            "Authorization": f"Bearer {video_api}",
            "Content-Type": "application/json"
    }
    response = requests.post(url, json=payload, headers=headers)
    try:
        return response.json()["choices"][0]["message"]["content"]
    except:
        return "链接失效"

def chat_webpage(wurl) -> str:
    """
    网页识别。
    :param wurl: 网页URL。
    :return: 网页识别结果。
    """
    max_seq_len = 131071

    if not wurl.startswith("http"):
        wurl = "https://"+wurl
    try:
        res = requests.get(wurl,headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        },timeout=10)
    except:
        return "链接失效"
    
    html = res.text

    if len(html) > max_seq_len:
        html = html[:max_seq_len]

    url = "https://api.siliconflow.cn/v1/chat/completions"

    payload = {
            "model": model,
            "messages": [
            {
            "role": "user",
            "content": [
                {
                    "type": "text", 
                    "text": f"请描述这个网页的内容，仅分析网页的主体内容，忽略网页的其他内容和技术相关的细节；仅返回主体内容的描述：{html}"
                }
            ]
            }
            ]
    }
    headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
    }
    response = requests.post(url, json=payload, headers=headers)
    try:
        return response.json()["choices"][0]["message"]["content"]
    except:
        return "链接失效"

def chat_json(content) -> str:
    """
    处理json字符串。
    :param content: json字符串。
    :return: 处理后的字符串。
    """
    url = "https://api.siliconflow.cn/v1/chat/completions"

    payload = {
            "model":model,
            "messages": [
            {
            "role": "user",
            "content": [
                {
                    "type": "text", 
                    "text": f"请分析这个json字符串的内容；如果有链接，则还需列出最重要的一个链接，忽略其他链接：{content}"
                }
            ]
            }
            ]
    }
    headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
    }
    response = requests.post(url, json=payload, headers=headers)
    try:
        return response.json()["choices"][0]["message"]["content"]
    except:
        return ""

def judge_search(content) -> bool:
    """
    判断是否是搜索。
    :param content: 用户输入的内容。
    :return: 是否是搜索。
    """
    url = "https://api.siliconflow.cn/v1/chat/completions"

    payload = {
            "model": "Qwen/Qwen2.5-7B-Instruct",
            "messages": [
            {
            "role": "user",
            "content": [
                {
                    "type": "text", 
                    "text": f"请判断这个内容AI是否需要搜索才能获取准确且最新的回答；如果需要搜索，则只返回1；如果不需要搜索，则只返回0：{content}"

                }
            ]
            }
            ]
    }
    headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
    }
    response = requests.post(url, json=payload, headers=headers)
    print("搜索判断:",response.json()["choices"][0]["message"]["content"])
    try:
        return int(response.json()["choices"][0]["message"]["content"]) == 1
    except:
        return False


def chat(content="", user_id=None, group_id=None, group_user_id=None,image=False,url=None,video=None):
    """
    与Ai进行对话。
    :param content: 用户输入的内容。
    :param user_id: 用户ID。
    :param group_id: 群组ID。
    :param group_user_id: 群组用户ID。
    :param image: 是否是图片。
    :param url: 图片URL。
    :param video: 视频url。
    :return: 机器人的回复。
    """
    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if user_id:
        user_id = str(user_id)
        prompt = load_prompt(user_id=user_id)
        if user_id not in user_messages:
            user_messages[user_id] = [{"role": "system", "content": prompt}]
        else:
            # 确保第一条系统消息始终是最新的提示词（包含最新的 helptext）
            if user_messages[user_id] and user_messages[user_id][0].get("role") == "system":
                user_messages[user_id][0]["content"] = prompt
            else:
                user_messages[user_id].insert(0, {"role": "system", "content": prompt})
        messages = user_messages[user_id]
    elif group_id:
        group_id = str(group_id)
        prompt = load_prompt(group_id=group_id)
        if group_id not in group_messages:
            group_messages[group_id] = [{"role": "system", "content": prompt}]
        else:
            # 确保第一条系统消息始终是最新的提示词（包含最新的 helptext）
            if group_messages[group_id] and group_messages[group_id][0].get("role") == "system":
                group_messages[group_id][0]["content"] = prompt
            else:
                group_messages[group_id].insert(0, {"role": "system", "content": prompt})
        messages = group_messages[group_id]
    else:
        messages = []

    if group_user_id:
        pre_text = f"用户{group_user_id}说："
    else:
        pre_text = ""

    if content.startswith("搜索") | ("搜索" in content) | judge_search(content):
        search_status = 1
        search_res = online_search(content)
    else:
        search_status = 0
        search_res = ""

    if image:
        response = chat_image(url)
        messages.append({"role": "user", "content":f"(当前时间：{now_time})"})
        if search_status == 1:
            messages.append({"role": "user", "content":f"{pre_text}"+"用户发送了一张图片，这是图片的描述："+response+" "+"这是联网搜索的结果："+search_res+"这是用户说的话："+content})
        else:
            messages.append({"role": "user", "content":f"{pre_text}"+"用户发送了一张图片，这是图片的描述："+response+" "+"这是用户说的话："+content})       
    elif video:
        response = chat_video(video)
        messages.append({"role": "user", "content":f"(当前时间：{now_time})"})
        messages.append({"role": "user", "content":f"{pre_text}"+ "这是视频的描述："+response+"这是用户说的话："+content})
    else:
        messages.append({"role": "user", "content":f"(当前时间：{now_time})"})
        if search_status == 1:
            messages.append({"role": "user", "content":f"{pre_text}"+ "这是联网搜索的结果："+search_res+"这是用户说的话："+content})
        else:
            messages.append({"role": "user", "content":f"{pre_text}"+content})
    
    des = ""
    pattern = r"(?:https?:\/\/)?(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z]{2,})+(?:\/[^\s?]*)?(?:\?[^\s]*)?"
    matches = re.findall(pattern, content)
    if matches:
        tot = 0 
        for match in matches:
            tot += 1
            des += f"第{tot}个链接{match}的描述："+chat_webpage(match) + "\n"
        messages.append({"role": "user", "content":f"{pre_text}"+des})
    
    # 保留最大历史记录
    if len(messages) > MAX_HISTORY_LENGTH:
        messages = [messages[0]] + messages[-MAX_HISTORY_LENGTH:]

    client = OpenAI(api_key=api_key,base_url=base_url)

    response = client.chat.completions.create(
        model=model,
        messages=messages,
        stream=False
    )
    
    assistant_response = response.choices[0].message.content
    try:
        import json_repair
        assistant_response = json_repair.repair(assistant_response)
    except:
        pass
    messages.append({"role": "assistant", "content": assistant_response})

    #保存数据
    with open("saved_message/user_messages.json","w",encoding="utf-8") as f:
        json.dump(user_messages,f,ensure_ascii=False,indent = 4)
    with open("saved_message/group_messages.json","w",encoding="utf-8") as f:
        json.dump(group_messages,f,ensure_ascii=False,indent = 4)

    return assistant_response

def summarize_group_text(text: str) -> str:
    text = text.strip()
    if not text:
        return "没有可总结的聊天记录喵~"
    client = OpenAI(api_key=api_key,base_url=base_url)
    system_prompt = "你是一个群聊记录总结助手，只根据提供的内容生成简洁的中文摘要。"
    user_prompt = (
        "下面是一整个QQ群的一段聊天记录，每一行代表一条消息，包含时间、群号、QQ号或昵称以及内容。\n"
        "请用中文总结出群聊的大致内容和几个主要话题，可以适当分点列出，不要复述所有细节：\n"
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
        client = OpenAI(api_key=api_key,base_url=base_url)
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
    return "没有可总结的聊天记录喵~"

def tts(content) -> MessageChain:
    file_path = os.path.join(cache_address , "tts/")
    os.makedirs(file_path, exist_ok=True)
    name = int(time.time())

    speech_file_path = os.path.join(file_path , f"{name}.mp3")

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.siliconflow.cn/v1"  #这也是硅基流动的模型，用同一个api
    )

    with client.audio.speech.with_streaming_response.create(
            model="fnlp/MOSS-TTSD-v0.5",
            voice=voice, #自定义音色
            # 用户输入信息
            input=remove_brackets_content(content),
            response_format="mp3"
    ) as response:
        response.stream_to_file(speech_file_path)

    message = MessageChain([
        Record(speech_file_path)
    ])
    return message

def upload_voice(file_path,name,text):
    """
    上传音频文件到硅基流动。
    :param file_path: 音频文件路径。
    :param name: 音频名称。
    :param text: 音频的文字内容。
    """
    url = "https://api.siliconflow.cn/v1/uploads/audio/voice"
    headers = {
        "Authorization": f"Bearer {api_key}"
        # 从 https://cloud.siliconflow.cn/account/ak 获取
    }
    files = {
        "file": open(fr"{file_path}", "rb")  # 参考音频文件
    }
    data = {
        "model": "fnlp/MOSS-TTSD-v0.5",  # 模型名称
        "customName": name,  # 参考音频名称
        "text": text  # 参考音频的文字内容
    }

    response = requests.post(url, headers=headers, files=files, data=data)

    print(response.status_code)
    print(response.json())  # 打印响应内容（如果是JSON格式）

if __name__ == "__main__":
    file_path = str(input("输入文件路径："))
    name = str(input("输入名称："))
    text = str(input("输入文字："))
    upload_voice(file_path,name,text)
