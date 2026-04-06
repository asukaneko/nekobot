"""
STT (Speech-to-Text) 语音识别服务
支持将语音转换为文字
"""
import os
import requests
from typing import Optional
from nbot.web.utils.config_loader import get_stt_model_config


def _get_stt_config():
    """获取STT配置，优先使用新架构的配置"""
    stt_config = get_stt_model_config()
    if stt_config and stt_config.get("api_key"):
        return {
            "api_key": stt_config.get("api_key"),
            "base_url": stt_config.get("base_url", ""),
            "model": stt_config.get("model", "whisper-1"),
            "language": stt_config.get("language", "zh"),
            "provider_type": stt_config.get("provider_type", "openai_compatible"),
        }
    # 回退到环境变量
    return {
        "api_key": os.getenv("API_KEY", ""),
        "base_url": os.getenv("BASE_URL", ""),
        "model": "whisper-1",
        "language": "zh",
        "provider_type": "openai_compatible",
    }


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
    api_key = config["api_key"]
    base_url = config["base_url"]
    model = config["model"]
    lang = language or config["language"]
    
    if not api_key:
        print("[STT] API Key not configured")
        return None
    
    try:
        # 构建请求URL
        if base_url:
            url = base_url.rstrip("/") + "/audio/transcriptions"
        else:
            url = "https://api.openai.com/v1/audio/transcriptions"
        
        headers = {
            "Authorization": f"Bearer {api_key}"
        }
        
        data = {
            "model": model,
            "language": lang,
            "response_format": "text"
        }
        
        with open(audio_file_path, "rb") as audio_file:
            files = {
                "file": (os.path.basename(audio_file_path), audio_file, "audio/mpeg")
            }
            response = requests.post(url, headers=headers, data=data, files=files, timeout=60)
        
        response.raise_for_status()
        result = response.text.strip()
        print(f"[STT] Transcription successful: {result[:100]}...")
        return result
        
    except Exception as e:
        print(f"[STT] Transcription failed: {e}")
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
    import tempfile
    
    try:
        # 下载音频文件
        response = requests.get(audio_url, timeout=30)
        response.raise_for_status()
        
        # 保存到临时文件
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp_file:
            tmp_file.write(response.content)
            tmp_path = tmp_file.name
        
        try:
            # 进行识别
            result = transcribe(tmp_path, language)
            return result
        finally:
            # 清理临时文件
            try:
                os.unlink(tmp_path)
            except:
                pass
                
    except Exception as e:
        print(f"[STT] Failed to transcribe from URL: {e}")
        return None
