"""
STT (Speech-to-Text) 语音识别服务
支持将语音转换为文字
"""
import logging
import os
import tempfile
from typing import Optional

from nbot.web.utils.config_loader import get_stt_model_config
from nbot.services.tts_config import normalize_stt_config

_log = logging.getLogger(__name__)


def _get_stt_config() -> dict:
    """获取STT配置，优先使用新架构的配置"""
    stt_config = get_stt_model_config()
    if stt_config and stt_config.get("api_key"):
        return normalize_stt_config(stt_config)
    # 回退到环境变量
    return {
        "api_key": os.getenv("API_KEY", ""),
        "base_url": os.getenv("BASE_URL", ""),
        "provider_type": "openai_compatible",
        "stt_provider": "",
        "stt_model": "whisper-1",
        "stt_language": "zh",
        "stt_url": "",
        "stt_headers": "",
    }


def _get_adapter_for_config(config: dict):
    """根据配置获取对应的 STT 适配器"""
    from nbot.services.stt_adapters import get_adapter
    provider = config.get("stt_provider") or config.get("provider_type", "")
    return get_adapter(provider)


def transcribe(audio_file_path: str, language: str = None) -> Optional[str]:
    """
    将语音文件转换为文字

    Args:
        audio_file_path: 音频文件路径
        language: 语言代码 (zh, en, ja, ko 等)，None则使用配置中的语言

    Returns:
        识别出的文字，失败返回None
    """
    config = _get_stt_config()
    api_key = config.get("api_key", "")
    provider = (
        config.get("stt_provider") or config.get("provider_type") or ""
    ).lower()
    is_local = provider in ("local", "faster_whisper")

    if not api_key and not is_local:
        _log.warning("[STT] API Key not configured")
        return None

    try:
        adapter = _get_adapter_for_config(config)
        result = adapter.transcribe(audio_file_path, config, language)
        if not result or not str(result).strip():
            return None
        _log.info("[STT] Transcription successful: %s...", str(result)[:100])
        return str(result).strip()
    except Exception as e:
        _log.error("[STT] Transcription failed: %s", e)
        return None


def transcribe_from_url(audio_url: str, language: str = None) -> Optional[str]:
    """
    从URL下载音频并转换为文字

    Args:
        audio_url: 音频文件URL
        language: 语言代码

    Returns:
        识别出的文字，失败返回None
    """
    import requests

    try:
        response = requests.get(audio_url, timeout=30)
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name

        try:
            result = transcribe(tmp_path, language)
            return result
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    except Exception as e:
        _log.error("[STT] Failed to transcribe from URL: %s", e)
        return None
