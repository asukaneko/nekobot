import configparser
import requests
import os
import base64
import json
import io
from PIL import Image
import imageio.v2 as imageio
from nbot.core import (
    build_chat_completion_payload,
    normalize_chat_completion_data,
    resolve_chat_completion_url,
)
from nbot.web.utils.config_loader import (
    get_vision_model_config,
    get_video_model_config,
)

config_parser = configparser.ConfigParser()
config_parser.read('config.ini', encoding='utf-8')

api_key = config_parser.get('ApiKey', 'api_key', fallback="")
base_url = config_parser.get('ApiKey', 'base_url', fallback="")
model = config_parser.get('ApiKey', 'model', fallback="")
MAX_HISTORY_LENGTH = config_parser.getint('chat', 'MAX_HISTORY_LENGTH', fallback=20)
pic_model = config_parser.get('pic', 'model', fallback="")
search_api_key = config_parser.get('search', 'api_key', fallback="")
search_api_url = config_parser.get('search', 'api_url', fallback="")
video_api = config_parser.get('video', 'api_key', fallback="")
silicon_api_key = config_parser.get('ApiKey', 'silicon_api_key', fallback="")
provider_type = config_parser.get('ApiKey', 'provider_type', fallback="openai_compatible")
supports_tools = config_parser.getboolean('ApiKey', 'supports_tools', fallback=True)
supports_reasoning = config_parser.getboolean('ApiKey', 'supports_reasoning', fallback=True)
supports_stream = config_parser.getboolean('ApiKey', 'supports_stream', fallback=True)


def resolve_runtime_api_key(configured_api_key: str = "", provider: str = "") -> str:
    provider_name = (provider or "").strip().lower()
    if provider_name == "minimax":
        return (
            os.getenv("MINIMAX_API_KEY")
            or os.getenv("API_KEY")
            or configured_api_key
        )
    if provider_name in {"siliconflow", "silicon"}:
        return (
            os.getenv("SILICON_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider_name in {"anthropic", "claude"}:
        return (
            os.getenv("ANTHROPIC_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider_name in {"google", "gemini"}:
        return (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider_name in {"openai", "openai_compatible", "custom", "deepseek"}:
        return (
            os.getenv("OPENAI_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    return configured_api_key or os.getenv("API_KEY")


def _load_shared_web_ai_config() -> dict:
    data_dir = os.path.join("data", "web")
    models_file = os.path.join(data_dir, "ai_models.json")
    config_file = os.path.join(data_dir, "ai_config.json")

    try:
        if os.path.exists(models_file):
            with open(models_file, "r", encoding="utf-8") as f:
                models_data = json.load(f)
            active_model_id = models_data.get("active_model_id")
            for item in models_data.get("models", []):
                if item.get("id") == active_model_id and item.get("enabled", True):
                    return item
    except Exception:
        pass

    try:
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass

    return {}


def get_runtime_ai_config() -> dict:
    shared = _load_shared_web_ai_config()
    effective = {
        "base_url": shared.get("base_url") or base_url,
        "model": shared.get("model") or model,
        "provider_type": shared.get("provider_type")
        or shared.get("provider")
        or provider_type
        or "openai_compatible",
        "supports_tools": shared.get("supports_tools", supports_tools),
        "supports_reasoning": shared.get("supports_reasoning", supports_reasoning),
        "supports_stream": shared.get("supports_stream", supports_stream),
    }
    effective["api_key"] = resolve_runtime_api_key(
        shared.get("api_key") or api_key,
        effective["provider_type"],
    )
    return effective


def refresh_runtime_ai_config() -> dict:
    global api_key, base_url, model, provider_type
    global supports_tools, supports_reasoning, supports_stream

    effective = get_runtime_ai_config()
    api_key = effective["api_key"]
    base_url = effective["base_url"]
    model = effective["model"]
    provider_type = effective["provider_type"]
    supports_tools = bool(effective["supports_tools"])
    supports_reasoning = bool(effective["supports_reasoning"])
    supports_stream = bool(effective["supports_stream"])

    client = globals().get("ai_client")
    if client is not None:
        client.api_key = api_key
        client.base_url = base_url
        client.model = model
        client.provider_type = provider_type
        client.supports_tools = supports_tools
        client.supports_reasoning = supports_reasoning
        client.supports_stream = supports_stream

    return effective

user_messages = {}
group_messages = {}

try:
    with open("saved_message/user_messages.json", "r", encoding="utf-8") as f:
        user_messages = json.load(f)
    with open("saved_message/group_messages.json", "r", encoding="utf-8") as f:
        group_messages = json.load(f)
except FileNotFoundError:
    os.makedirs("saved_message", exist_ok=True)


class AIClient:
    def __init__(self, api_key: str, base_url: str, model: str, pic_model: str,
                 search_api_key: str, search_api_url: str, video_api: str, silicon_api_key: str,
                 provider_type: str = "openai_compatible",
                 supports_tools: bool = True,
                 supports_reasoning: bool = True,
                 supports_stream: bool = True):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.pic_model = pic_model
        self.search_api_key = search_api_key
        self.search_api_url = search_api_url
        self.video_api = video_api
        self.silicon_api_key = silicon_api_key
        self.provider_type = provider_type or "openai_compatible"
        self.supports_tools = bool(supports_tools)
        self.supports_reasoning = bool(supports_reasoning)
        self.supports_stream = bool(supports_stream)

    @staticmethod
    def clean_response(content: str) -> str:
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
        stream = bool(stream and self.supports_stream)
        url_base = (self.base_url or "").rstrip("/")
        if not url_base:
            raise ValueError("base_url 未配置")
        
        # 检查是否为Anthropic provider
        is_anthropic = self.provider_type == "anthropic" or "anthropic" in url_base.lower()
        
        if is_anthropic:
            # 使用Anthropic Messages API格式
            from nbot.services.anthropic_adapter import (
                build_anthropic_payload,
                parse_anthropic_response,
                get_anthropic_headers,
            )
            
            url = f"{url_base}/v1/messages"
            headers = get_anthropic_headers(self.api_key)
            payload = build_anthropic_payload(
                model=model or self.model,
                messages=messages,
                max_tokens=4096,
                stream=stream,
            )
            
            if stream:
                # 流式响应模式
                resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
                resp.raise_for_status()
                return self._stream_anthropic_response(resp)
            else:
                # 非流式响应模式
                resp = requests.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
                
                parsed = parse_anthropic_response(data)
                content = parsed["content"]
                usage = parsed.get("usage", {})
                
                Message = type("Message", (), {})
                Choice = type("Choice", (), {})
                Usage = type("Usage", (), {})
                Resp = type("Resp", (), {})
                msg_obj = Message()
                msg_obj.content = content
                choice_obj = Choice()
                choice_obj.message = msg_obj
                usage_obj = Usage()
                usage_obj.prompt_tokens = usage.get("prompt_tokens", 0)
                usage_obj.completion_tokens = usage.get("completion_tokens", 0)
                usage_obj.total_tokens = usage.get("total_tokens", 0)
                resp_obj = Resp()
                resp_obj.choices = [choice_obj]
                resp_obj.usage = usage_obj
                return resp_obj
        else:
            # 使用OpenAI兼容格式
            url = resolve_chat_completion_url(
                self.base_url,
                model=model or self.model or "",
                provider_type=self.provider_type,
            )
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }
            payload = build_chat_completion_payload(
                model or self.model,
                messages,
                base_url=self.base_url,
                provider_type=self.provider_type,
                stream=stream,
            )
            
            if stream:
                # 流式响应模式
                resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=300)
                resp.raise_for_status()
                return self._stream_response(resp)
            else:
                # 非流式响应模式
                resp = requests.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                if not data.get("choices"):
                    print(f"[DEBUG] API响应没有choices: {data}")

                normalized = normalize_chat_completion_data(
                    data,
                    base_url=self.base_url or "",
                    model=model or self.model or "",
                    provider_type=self.provider_type,
                )
                content = normalized.content

                # 获取 usage 信息
                usage = normalized.usage or {}
                prompt_tokens = usage.get("prompt_tokens", 0) if usage else 0
                completion_tokens = usage.get("completion_tokens", 0) if usage else 0
                total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens) if usage else prompt_tokens + completion_tokens

                Message = type("Message", (), {})
                Choice = type("Choice", (), {})
                Usage = type("Usage", (), {})
                Resp = type("Resp", (), {})
                msg_obj = Message()
                msg_obj.content = content
                choice_obj = Choice()
                choice_obj.message = msg_obj
                usage_obj = Usage()
                usage_obj.prompt_tokens = prompt_tokens
                usage_obj.completion_tokens = completion_tokens
                usage_obj.total_tokens = total_tokens
                resp_obj = Resp()
                resp_obj.choices = [choice_obj]
                resp_obj.usage = usage_obj
                return resp_obj
    
    def _stream_response(self, resp):
        """处理流式响应，返回一个生成器"""
        import json
        
        for line in resp.iter_lines():
            if not line:
                continue
            
            line_text = line.decode('utf-8') if isinstance(line, bytes) else line
            
            # 跳过 ping/pong 等非数据行
            if not line_text.startswith('data: '):
                continue
            
            data_str = line_text[6:].strip()
            if data_str == '[DONE]':
                break
            
            try:
                data = json.loads(data_str)
                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        yield content
            except json.JSONDecodeError:
                continue

    def _stream_anthropic_response(self, resp):
        """处理Anthropic流式响应，返回一个生成器"""
        import json
        from nbot.services.anthropic_adapter import parse_anthropic_stream_chunk
        
        for line in resp.iter_lines():
            if not line:
                continue
            
            line_text = line.decode('utf-8') if isinstance(line, bytes) else line
            
            # Anthropic的流式格式是event: xxx\ndata: xxx\n\n
            if line_text.startswith('event: '):
                continue
            
            if not line_text.startswith('data: '):
                continue
            
            data_str = line_text[6:].strip()
            if data_str == '[DONE]':
                break
            
            try:
                data = json.loads(data_str)
                parsed = parse_anthropic_stream_chunk(data)
                
                if parsed and parsed.get("type") == "content":
                    yield parsed.get("content", "")
            except json.JSONDecodeError:
                continue


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
            "Authorization": f"Bearer {self.silicon_api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": model_name,
            "messages": messages
        }
        return requests.post(url, json=payload, headers=headers)

    def describe_image(self, image_url: str, text: str = None) -> str:
        print(f"[图片识别] 开始识别图片, URL: {image_url}")
        
        # 获取图片理解模型配置
        vision_config = get_vision_model_config()
        if vision_config and vision_config.get("api_key"):
            # 使用配置的图片理解模型
            api_key = vision_config.get("api_key")
            base_url = vision_config.get("base_url", "")
            model = vision_config.get("model", "zai-org/GLM-4.6V")
            provider_type = vision_config.get("provider_type", "openai_compatible")
            system_prompt = vision_config.get("system_prompt", "请详细描述这张图片的内容。")
            
            messages = [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": image_url}
                        },
                        {
                            "type": "text",
                            "text": text or system_prompt
                        }
                    ]
                }
            ]
            
            try:
                url = resolve_chat_completion_url(base_url, model=model, provider_type=provider_type)
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                payload = build_chat_completion_payload(
                    model,
                    messages,
                    base_url=base_url,
                    provider_type=provider_type,
                )
                response = requests.post(url, json=payload, headers=headers, timeout=60)
                response.raise_for_status()
                result = self.clean_response(response.json()["choices"][0]["message"]["content"])
                print(f"[图片识别] 识别成功(使用配置模型), 结果: {result[:100]}...")
                return result
            except Exception as e:
                print(f"[图片识别] 使用配置模型失败, 回退到默认模型: {e}")
        
        # 回退到默认的silicon_chat
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": text or "请详细描述这张图片的内容。"}
                ]
            }
        ]
        response = self.silicon_chat(self.pic_model, messages)
        try:
            result = self.clean_response(response.json()["choices"][0]["message"]["content"])
            print(f"[图片识别] 识别成功, 结果: {result[:100]}...")
            return result
        except Exception as e:
            print(f"[图片识别] 识别失败, 错误: {e}, 响应: {response.text}")
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
        try:
            response = self.silicon_chat("Qwen/Qwen2.5-7B-Instruct", messages)
            result = response.json()
            print(f"[DEBUG] should_search API响应: {result}")
            return int(self.clean_response(result["choices"][0]["message"]["content"])) == 1
        except Exception as e:
            print(f"[DEBUG] should_search 错误: {e}")
            return False

    def should_reply(self, content: str) -> float:
        messages = [
            {
                "role": "system",
                "content": "你是一个对话助手，需要根据群聊上下文和机器人的人设来判断是否应该回复当前消息。请输出 0 到 1 之间的一个小数，表示'应该回复程度'：0 表示完全不应该回复，1 表示非常应该回复，只输出这个数字，不要输出其他内容。"
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

    def describe_video(self, video_url: str, text: str = None) -> str:
        print(f"[视频识别] 开始识别视频, URL: {video_url}")
        
        # 获取视频理解模型配置
        video_config = get_video_model_config()
        if video_config and video_config.get("api_key"):
            api_key = video_config.get("api_key")
            base_url = video_config.get("base_url", "")
            model = video_config.get("model", "zai-org/GLM-4.6V")
            provider_type = video_config.get("provider_type", "openai_compatible")
            system_prompt = video_config.get("system_prompt", "请分析这个视频的内容。")
        else:
            # 使用默认配置
            api_key = self.api_key
            base_url = self.base_url
            model = "zai-org/GLM-4.6V"
            provider_type = self.provider_type
            system_prompt = "请分析这个视频的内容。"
        
        try:
            url = resolve_chat_completion_url(base_url, model=model, provider_type=provider_type)
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "video_url", "video_url": {"url": video_url}},
                            {"type": "text", "text": text or system_prompt}
                        ]
                    }
                ]
            }
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }
            response = requests.post(url, json=payload, headers=headers, timeout=120)
            response.raise_for_status()
            result = self.clean_response(response.json()["choices"][0]["message"]["content"])
            print(f"[视频识别] 识别成功, 结果: {result[:100]}...")
            return result
        except Exception as e:
            print(f"[视频识别] 识别失败: {e}")
            return f"链接失效,错误: {str(e)}"


ai_client = AIClient(
    api_key=api_key,
    base_url=base_url,
    model=model,
    pic_model=pic_model,
    search_api_key=search_api_key,
    search_api_url=search_api_url,
    video_api=video_api,
    silicon_api_key=silicon_api_key,
    provider_type=provider_type,
    supports_tools=supports_tools,
    supports_reasoning=supports_reasoning,
    supports_stream=supports_stream,
)
