import configparser
import logging
import os
import time

from nbot.services.tts_config import get_tts_config, get_tts_config_from_server
from nbot.web.utils.config_loader import get_tts_model_config

_log = logging.getLogger(__name__)

config_parser = configparser.ConfigParser()
config_parser.read('config.ini', encoding='utf-8')

cache_address = os.path.abspath(
    os.getenv("CACHE_ADDRESS") or config_parser.get("cache", "cache_address", fallback="cache")
)


def _get_legacy_tts_config():
    """获取TTS配置，返回统一字段格式"""
    tts_config = get_tts_model_config()
    if tts_config and tts_config.get("api_key"):
        voice = (tts_config.get("tts_voice") or tts_config.get("voice") or "").strip()
        if not voice or voice == "default":
            voice = "alloy"
        return {
            "api_key": tts_config.get("api_key"),
            "base_url": tts_config.get("base_url") or "https://api.openai.com/v1",
            "proxy_url": tts_config.get("proxy_url") or "",
            "tts_provider": tts_config.get("tts_provider", ""),
            "provider_type": tts_config.get("provider_type", "openai_compatible"),
            "tts_url": tts_config.get("tts_url") or "",
            "tts_model": tts_config.get("tts_model") or tts_config.get("model") or "gpt-4o-mini-tts",
            "tts_voice": voice,
            "tts_speed": tts_config.get("tts_speed") or tts_config.get("speed") or 1.0,
            "tts_pitch": tts_config.get("tts_pitch") or tts_config.get("pitch") or 1.0,
            "tts_volume": tts_config.get("tts_volume") or tts_config.get("volume") or 1.0,
            "tts_format": tts_config.get("tts_format") or "mp3",
            "tts_headers": tts_config.get("tts_headers") or "",
            "tts_body_template": tts_config.get("tts_body_template") or "",
        }
    return {
        "api_key": config_parser.get('ApiKey', 'api_key', fallback=""),
        "base_url": "https://api.openai.com/v1",
        "provider_type": "openai_compatible",
        "tts_url": "",
        "tts_model": "gpt-4o-mini-tts",
        "tts_voice": "alloy",
        "tts_speed": 1.0,
        "tts_pitch": 1.0,
        "tts_volume": 1.0,
        "tts_format": "mp3",
        "tts_headers": "",
        "tts_body_template": "",
    }


def _get_tts_config():
    try:
        from nbot.web.server import WebChatServer

        server = WebChatServer.get_instance()
        if server:
            return get_tts_config_from_server(server)
    except Exception:
        pass

    return get_tts_config()


def remove_brackets_content(text: str) -> str:
    import re
    text = re.sub(r'（.*?）', '', text)
    text = re.sub(r'【.*?】', '', text)
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'\{.*?\}', '', text)
    text = re.sub(r'\「.*?\」', '', text)
    text = text.replace('\n', ' ').replace('\r', ' ')
    return text.strip()


def tts(content: str):
    from ncatbot.core.element import MessageChain, Record

    file_path = os.path.join(cache_address, "tts/")
    os.makedirs(file_path, exist_ok=True)
    speech_file_path = os.path.join(file_path, f"{int(time.time())}.mp3")

    tts_config = _get_tts_config()
    clean_text = remove_brackets_content(content)

    try:
        from nbot.services.tts_adapters import get_adapter
        provider = tts_config.get("tts_provider") or tts_config.get("provider_type", "")
        adapter = get_adapter(provider)
        adapter.synthesize(clean_text, tts_config, speech_file_path)

        # 记录 TTS 用量（按字符数估算 token，TTS 不返回标准 token usage）
        try:
            from nbot.core.token_stats import get_token_stats_manager, PURPOSE_TTS
            char_count = len(clean_text)
            if char_count > 0:
                stats_mgr = get_token_stats_manager()
                stats_mgr.record_usage(
                    prompt_tokens=char_count,
                    completion_tokens=0,
                    total_tokens=char_count,
                    model=tts_config.get("tts_model") or "",
                    channel_type="tts",
                    source="tts",
                    purpose=PURPOSE_TTS,
                )
        except Exception:
            pass

        return MessageChain([Record(speech_file_path)])
    except Exception as e:
        _log.error("TTS生成失败(tts): %s", str(e))
        return MessageChain([])


def generate_tts_audio(content: str) -> str | None:
    """生成 TTS 音频文件并返回文件路径

    用于 Gateway 频道集成，返回音频文件路径而非 MessageChain。

    Args:
        content: 要转换为语音的文本内容

    Returns:
        音频文件路径，失败时返回 None
    """
    file_path = os.path.join(cache_address, "tts/")
    os.makedirs(file_path, exist_ok=True)
    speech_file_path = os.path.join(file_path, f"{int(time.time())}.mp3")

    tts_config = _get_tts_config()
    clean_text = remove_brackets_content(content)

    try:
        from nbot.services.tts_adapters import get_adapter
        provider = tts_config.get("tts_provider") or tts_config.get("provider_type", "")
        _log.info("TTS 开始生成语音 provider=%s model=%s", provider, tts_config.get("tts_model", ""))
        adapter = get_adapter(provider)
        adapter.synthesize(clean_text, tts_config, speech_file_path)

        # 记录 TTS 用量
        try:
            from nbot.core.token_stats import get_token_stats_manager, PURPOSE_TTS
            char_count = len(clean_text)
            if char_count > 0:
                stats_mgr = get_token_stats_manager()
                stats_mgr.record_usage(
                    prompt_tokens=char_count,
                    completion_tokens=0,
                    total_tokens=char_count,
                    model=tts_config.get("tts_model") or "",
                    channel_type="tts",
                    source="tts",
                    purpose=PURPOSE_TTS,
                )
        except Exception as stats_err:
            _log.debug("[TTS] 记录 token 用量失败: %s", stats_err)

        if os.path.exists(speech_file_path):
            file_size = os.path.getsize(speech_file_path)
            _log.info("TTS 语音生成成功 path=%s size=%d", speech_file_path, file_size)
            return speech_file_path
        _log.warning("TTS 语音文件未生成 path=%s", speech_file_path)
        return None
    except Exception as e:
        _log.error("TTS生成失败: %s", str(e))
        return None


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

    from nbot.core.model_proxy import model_proxy_request_kwargs

    response = requests.post(
        url,
        headers=headers,
        files=files,
        data=data,
        **model_proxy_request_kwargs(tts_config.get("proxy_url", "")),
    )
    print(response.status_code)
    print(response.json())


if __name__ == "__main__":
    file_path = str(input("输入文件路径："))
    name = str(input("输入名称："))
    text = str(input("输入文字："))
    upload_voice(file_path, name, text)
