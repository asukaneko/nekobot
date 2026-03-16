import os, time
import configparser
from ncatbot.core.element import Record, MessageChain

config_parser = configparser.ConfigParser()
config_parser.read('config.ini', encoding='utf-8')

cache_address = config_parser.get('cache', 'cache_address')
silicon_api_key = config_parser.get('ApiKey', 'silicon_api_key')
voice = config_parser.get('voice', 'voice')


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

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=silicon_api_key,
            base_url="https://api.siliconflow.cn/v1"
        )

        with client.audio.speech.with_streaming_response.create(
                model="fnlp/MOSS-TTSD-v0.5",
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
    url = "https://api.siliconflow.cn/v1/uploads/audio/voice"
    headers = {
        "Authorization": f"Bearer {silicon_api_key}"
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
