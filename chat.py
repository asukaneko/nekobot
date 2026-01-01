# 提示词和聊天记录保存在项目目录下，与代码直接关联，不建议修改。保存的图片可以在配置文件中配置
#  .saved_message
#   |--- ...
#  .prompts
#   |---... 
import configparser,requests,os,base64,time,json,datetime,re,io
from PIL import Image
import imageio.v2 as imageio
from ncatbot.core.element import Record,MessageChain
from life_core import life_system

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
pic_model = config_parser.get('pic', 'model')
cache_address = config_parser.get('cache','cache_address')
voice = config_parser.get('voice','voice')
search_api_key = config_parser.get('search','api_key')
search_api_url = config_parser.get('search','api_url')
video_api = config_parser.get('video','api_key')


class AIClient:
    def __init__(self, api_key: str, base_url: str, model: str, pic_model: str, search_api_key: str, search_api_url: str, video_api: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.pic_model = pic_model
        self.search_api_key = search_api_key
        self.search_api_url = search_api_url
        self.video_api = video_api

    @staticmethod
    def clean_response(content: str) -> str:
        """剥离 markdown 代码块标记"""
        if not content:
            return ""
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

    def chat_completion(self, messages, model: str = None, stream: bool = False):
        url_base = (self.base_url or "").rstrip("/")
        if not url_base:
            raise ValueError("base_url 未配置")
        url = f"{url_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model or self.model,
            "messages": messages,
            "stream": stream
        }
        resp = requests.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        content = ""
        try:
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception:
            content = ""
        Message = type("Message", (), {})
        Choice = type("Choice", (), {})
        Resp = type("Resp", (), {})
        msg_obj = Message()
        msg_obj.content = content
        choice_obj = Choice()
        choice_obj.message = msg_obj
        resp_obj = Resp()
        resp_obj.choices = [choice_obj]
        return resp_obj

    def summarize_text(self, system_prompt: str, user_prompt: str, model: str = None) -> str:
        response = self.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            stream=False,
        )
        return self.clean_response(response.choices[0].message.content)

    def silicon_chat(self, model_name: str, messages):
        url = "https://api.siliconflow.cn/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": messages
        }
        return requests.post(url, json=payload, headers=headers)

    def describe_image(self, image_url: str, text: str) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": image_url
                        }
                    },
                    {
                        "type": "text",
                        "text": text
                    }
                ]
            }
        ]
        response = self.silicon_chat(self.pic_model, messages)
        try:
            return self.clean_response(response.json()["choices"][0]["message"]["content"])
        except Exception:
            return "链接失效"

    def gif_to_mp4_data_url(self, image_url: str, fps: int = 10) -> str:
        try:
            res = requests.get(image_url, timeout=10)
            if res.status_code != 200:
                return ""
            buf_in = io.BytesIO(res.content)
            frames = imageio.mimread(buf_in, format="gif")
            if not frames:
                return ""
            buf_out = io.BytesIO()
            imageio.mimsave(buf_out, frames, format="ffmpeg", fps=fps)
            mp4_bytes = buf_out.getvalue()
            if not mp4_bytes:
                return ""
            b64 = base64.b64encode(mp4_bytes).decode("utf-8")
            return "data:video/mp4;base64," + b64
        except Exception:
            return ""

    def describe_gif(self, image_url: str, max_frames: int = 10) -> str:
        try:
            res = requests.get(image_url, timeout=10)
            if res.status_code != 200:
                return "链接失效"
            img = Image.open(io.BytesIO(res.content))
            total = getattr(img, "n_frames", 1)
            if total <= 1:
                return self.describe_image(image_url, "请描述这个图片的内容，仅作描述，不要分析内容")
            
            content_list = []
            used = set()
            # 均匀采样，减少帧数以保证请求效率和模型理解效果
            count = min(max_frames, total)
            for i in range(count):
                idx = int(i * total / count)
                if idx >= total:
                    idx = total - 1
                if idx in used:
                    continue
                used.add(idx)
                try:
                    img.seek(idx)
                    frame = img.convert("RGB")
                    buf = io.BytesIO()
                    frame.save(buf, format="PNG")
                    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                    data_url = "data:image/png;base64," + b64
                    content_list.append({
                        "type": "image_url",
                        "image_url": {
                            "url": data_url
                        }
                    })
                except Exception:
                    continue
            
            if not content_list:
                return "解析失败"

            # 添加整体分析的提示词
            content_list.append({
                "type": "text",
                "text": "以上是一个 GIF 动画的连续帧序列。请作为一个整体分析这个动画，描述其中发生的动作、情节以及角色的情绪。"
            })

            messages = [
                {
                    "role": "user",
                    "content": content_list
                }
            ]
            
            response = self.silicon_chat(self.pic_model, messages)
            try:
                return self.clean_response(response.json()["choices"][0]["message"]["content"])
            except Exception:
                return "解析失败"
        except Exception:
            return "解析失败"

    def describe_gif_as_video(self, image_url: str) -> str:
        data_url = self.gif_to_mp4_data_url(image_url)
        if data_url:
            result = self.describe_video(data_url)
            if result and not result.startswith("链接失效"):
                return result
        return self.describe_gif(image_url)

    def describe_webpage_html(self, html: str) -> str:
        messages = [
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
        response = self.silicon_chat(self.model, messages)
        try:
            return self.clean_response(response.json()["choices"][0]["message"]["content"])
        except Exception:
            return "链接失效"

    def analyze_json(self, content: str) -> str:
        messages = [
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
        response = self.silicon_chat(self.model, messages)
        try:
            return self.clean_response(response.json()["choices"][0]["message"]["content"])
        except Exception:
            return ""

    def should_search(self, content: str) -> bool:
        messages = [
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
        response = self.silicon_chat("Qwen/Qwen2.5-7B-Instruct", messages)
        try:
            return int(self.clean_response(response.json()["choices"][0]["message"]["content"])) == 1
        except Exception:
            return False

    def should_reply(self, content: str) -> float:
        messages = [
            {
                "role": "system",
                "content": "你是一个对话助手，需要根据群聊上下文和机器人的人设来判断是否应该回复当前消息。请输出 0 到 1 之间的一个小数，表示“应该回复程度”：0 表示完全不应该回复，1 表示非常应该回复，只输出这个数字，不要输出其他内容。"
            },
            {
                "role": "user",
                "content": content
            }
        ]
        response = self.chat_completion(messages=messages, stream=False)
        try:
            score_str = self.clean_response(response.choices[0].message.content)
            score = float(score_str)
            if score < 0:
                score = 0.0
            if score > 1:
                score = 1.0
            return score
        except Exception:
            return 0.0

    def search(self, content: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.search_api_key}"
        }
        data = {
            "query": content,
            "query_rewrite": True,
            "top_k": 6
        }
        try:
            response = requests.post(self.search_api_url, headers=headers, json=data)
            return str(response.json()["result"]["search_result"])
        except Exception:
            return ""

    def describe_video(self, video_url: str) -> str:
        url = self.base_url + "/chat/completions"
        payload = {
            "model": "zai-org/GLM-4.6V",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "video_url",
                            "video_url": {
                                "url": video_url
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
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        response = requests.post(url, json=payload, headers=headers)
        try:
            return self.clean_response(response.json()["choices"][0]["message"]["content"])
        except Exception:
            return "链接失效,完整json:" + str(response.json())


ai_client = AIClient(
    api_key=api_key,
    base_url=base_url,
    model=model,
    pic_model=pic_model,
    search_api_key=search_api_key,
    search_api_url=search_api_url,
    video_api=video_api,
)


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
    return ai_client.search(content)

def chat_image(iurl) -> str:
    """
    图片识别。
    :param url: 图片URL。
    :return: 图片识别结果。
    """
    return ai_client.describe_image(iurl, "请描述这个图片的内容，仅作描述，不要分析内容")

def chat_gif(iurl) -> str:
    return ai_client.describe_gif_as_video(iurl)

def chat_video(vurl) -> str:
    """
    视频识别。
    :param url: 视频URL。
    :return: 视频识别结果。
    """
    return ai_client.describe_video(vurl)

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

    return ai_client.describe_webpage_html(html)

def chat_json(content) -> str:
    """
    处理json字符串。
    :param content: json字符串。
    :return: 处理后的字符串。
    """
    return ai_client.analyze_json(content)

def judge_search(content) -> bool:
    """
    判断是否是搜索。
    :param content: 用户输入的内容。
    :return: 是否是搜索。
    """
    return ai_client.should_search(content)

def judge_reply(content) -> float:
    return ai_client.should_reply(content)


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
        # 注入生命周期 Prompt
        prompt += life_system.get_prompt_suffix(user_id=user_id)
        
        if user_id not in user_messages:
            user_messages[user_id] = [{"role": "system", "content": prompt}]
        else:
            # 确保第一条系统消息始终是最新的提示词（包含最新的 helptext 和生命状态）
            if user_messages[user_id] and user_messages[user_id][0].get("role") == "system":
                user_messages[user_id][0]["content"] = prompt
            else:
                user_messages[user_id].insert(0, {"role": "system", "content": prompt})
        messages = user_messages[user_id]
    elif group_id:
        group_id = str(group_id)
        prompt = load_prompt(group_id=group_id)
        # 注入生命周期 Prompt
        prompt += life_system.get_prompt_suffix(group_id=group_id)
        
        if group_id not in group_messages:
            group_messages[group_id] = [{"role": "system", "content": prompt}]
        else:
            # 确保第一条系统消息始终是最新的提示词（包含最新的 helptext 和生命状态）
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

    response = ai_client.chat_completion(
        model=model,
        messages=messages,
        stream=False
    )
    assistant_response = response.choices[0].message.content
    
    # 处理 markdown 代码块包裹的情况
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

    # 记录由发送函数统一处理，此处不再重复添加
    # messages.append({"role": "assistant", "content": assistant_response})

    #保存数据
    with open("saved_message/user_messages.json","w",encoding="utf-8") as f:
        json.dump(user_messages,f,ensure_ascii=False,indent = 4)
    with open("saved_message/group_messages.json","w",encoding="utf-8") as f:
        json.dump(group_messages,f,ensure_ascii=False,indent = 4)

    return assistant_response

def _record_message(role, content, user_id=None, group_id=None):
    """
    统一记录消息到历史记录中的内部函数。
    """
    if not content:
        return

    now_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 如果是用户消息，自动带上时间戳（模拟 chat 函数的行为）
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

    # 保存数据
    try:
        with open("saved_message/user_messages.json", "w", encoding="utf-8") as f:
            json.dump(user_messages, f, ensure_ascii=False, indent=4)
        with open("saved_message/group_messages.json", "w", encoding="utf-8") as f:
            json.dump(group_messages, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"保存历史记录失败: {e}")

# 记录最后一次写入的内容，用于简单的重复过滤
last_log_entry = {}

def log_to_group_full_file(group_id, user_id, nickname, content, timestamp=None):
    """
    将消息记录到 group_full 文本文件中，用于每日总结。
    """
    if not group_id or not content:
        return
    
    group_id = str(group_id)
    user_id = str(user_id)
    content = str(content).strip()
    
    # 简单的重复记录过滤：如果同一个群在 1 秒内发送了完全相同的内容，则忽略
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
    """
    手动记录机器人的回复到历史记录中。
    """
    _record_message("assistant", content, user_id, group_id)

def record_user_message(content, user_id=None, group_id=None):
    """
    手动记录用户的消息到历史记录中。
    """
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
