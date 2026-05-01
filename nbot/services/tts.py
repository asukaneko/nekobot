import os
import time
import configparser
from ncatbot.core.element import Record, MessageChain
from nbot.web.utils.config_loader import get_tts_model_config

config_parser = configparser.ConfigParser()
config_parser.read('config.ini', encoding='utf-8')

cache_address = config_parser.get('cache', 'cache_address')


def _get_tts_config():
    """获取TTS配置，优先使用新架构的配置"""
    tts_config = get_tts_model_config()
    if tts_config and tts_config.get("api_key"):
        return {
            "api_key": tts_config.get("api_key"),
            "base_url": tts_config.get("base_url", "https://api.siliconflow.cn/v1"),
            "model": tts_config.get("model", "fnlp/MOSS-TTSD-v0.5"),
            "voice": tts_config.get("voice", "default"),
            "speed": tts_config.get("speed", 1.0),
        }
    # 回退到传统配置
    return {
        "api_key": config_parser.get('ApiKey', 'silicon_api_key', fallback=""),
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "fnlp/MOSS-TTSD-v0.5",
        "voice": config_parser.get('voice', 'voice', fallback="fnlp/MOSS-TTSD-v0.5:diana").split(":")[-1],
        "speed": 1.0,
    }


def remove_brackets_content(text: str) -> str:
    import re
    text = re.sub(r'（.*?）', '', text)
    text = re.sub(r'【.*?】', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'\{.*?\}', '', text)
    text = re.sub(r'\「.*?\」', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text.strip()


def tts(content: str) -> MessageChain:
    file_path = os.path.join(cache_address, "tts/")
    os.makedirs(file_path, exist_ok=True)
    name = int(time.time())

    speech_file_path = os.path.join(file_path, f"{name}.mp3")

    # 获取TTS配置
    tts_config = _get_tts_config()
    api_key = tts_config["api_key"]
    base_url = tts_config["base_url"]
    model = tts_config["model"]
    voice = tts_config["voice"]

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=api_key,
            base_url=base_url
        )

        with client.audio.speech.with_streaming_response.create(
                model=model,
                voice=voice,
                input=remove_brackets_content(content),
                response_format="mp3"
        ) as response:
            response.stream_to_file(speech_file_path)

        message = MessageChain([
            Record(speech_file_path)
        ])
        return message
    except Exception as e:
        print(f"TTS生成失败: {e}")
        return MessageChain([])


def upload_voice(file_path: str, name: str, text: str):
    import requests

    tts_config = _get_tts_config()
    api_key = tts_config.get("api_key", "")

    if not api_key:
        raise ValueError("未配置 SiliconFlow API Key")

    url = "https://api.siliconflow.cn/v1/uploads/audio/voice"
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    files = {
        "file": open(fr"{file_path}", "rb")
    }
    data = {
        "model": "fnlp/MOSS-TTSD-v0.5",
        "customName": name,
        "text": text
    }

    response = requests.post(url, headers=headers, files=files, data=data)
    print(response.status_code)
    print(response.json())


if __name__ == "__main__":
    file_path = str(input("输入文件路径："))
    name = str(input("输入名称："))
    text = str(input("输入文字："))
    upload_voice(file_path, name, text)
