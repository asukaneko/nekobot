import configparser
import os

from dotenv import load_dotenv


_ENV_PATH = os.path.join(
    os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ),
    ".env",
)

if os.path.exists(_ENV_PATH):
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()


def _read_config():
    config_parser = configparser.ConfigParser()
    config_parser.read("config.ini", encoding="utf-8")
    return config_parser


def resolve_runtime_api_key(configured_api_key: str = "", provider_type: str = "") -> str:
    provider = (provider_type or "").strip().lower()
    if provider == "minimax":
        return os.getenv("MINIMAX_API_KEY") or os.getenv("API_KEY") or configured_api_key
    if provider in {"siliconflow", "silicon"}:
        return (
            os.getenv("SILICON_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider in {"anthropic", "claude"}:
        return (
            os.getenv("ANTHROPIC_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider in {"google", "gemini"}:
        return (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider in {"openai", "openai_compatible", "custom", "deepseek"}:
        return (
            os.getenv("OPENAI_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    return configured_api_key or os.getenv("API_KEY")


def load_config():
    config_parser = _read_config()

    bot_uin = os.getenv("BOT_UIN") or config_parser.get(
        "BotConfig", "bot_uin", fallback=""
    )
    root = os.getenv("ROOT") or config_parser.get("BotConfig", "root", fallback="")

    return bot_uin, root


def get_api_config():
    config_parser = _read_config()

    provider_type = os.getenv("PROVIDER_TYPE") or config_parser.get(
        "ApiKey", "provider_type", fallback="openai_compatible"
    )
    api_key = resolve_runtime_api_key(
        config_parser.get("ApiKey", "api_key", fallback=""),
        provider_type,
    )
    base_url = os.getenv("BASE_URL") or config_parser.get(
        "ApiKey",
        "base_url",
        fallback="https://api.minimaxi.com/v1/text/chatcompletion_v2",
    )
    model = os.getenv("MODEL") or config_parser.get(
        "ApiKey", "model", fallback="MiniMax-M2.7"
    )
    silicon_api_key = os.getenv("SILICON_API_KEY") or config_parser.get(
        "ApiKey", "silicon_api_key", fallback=""
    )

    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "provider_type": provider_type,
        "silicon_api_key": silicon_api_key,
    }


def get_web_config():
    config_parser = _read_config()

    password = os.getenv("WEB_PASSWORD") or config_parser.get(
        "web", "password", fallback=""
    )

    return {"password": password}


def get_chat_config():
    config_parser = _read_config()

    max_history_length = os.getenv("MAX_HISTORY_LENGTH") or config_parser.get(
        "chat", "MAX_HISTORY_LENGTH", fallback="50"
    )

    return {"max_history_length": int(max_history_length)}


def get_pic_config():
    config_parser = _read_config()

    model = os.getenv("PIC_MODEL") or config_parser.get(
        "pic", "model", fallback="zai-org/GLM-4.6V"
    )

    return {"model": model}


def get_cache_config():
    config_parser = _read_config()

    cache_address = os.getenv("CACHE_ADDRESS") or config_parser.get(
        "cache", "cache_address", fallback="./cache"
    )

    return {"cache_address": cache_address}


def get_voice_config():
    config_parser = _read_config()

    voice = os.getenv("VOICE") or config_parser.get(
        "voice", "voice", fallback="fnlp/MOSS-TTSD-v0.5:diana"
    )

    return {"voice": voice}


def get_search_config():
    config_parser = _read_config()

    api_key = os.getenv("SEARCH_API_KEY") or config_parser.get(
        "search", "api_key", fallback=""
    )
    api_url = os.getenv("SEARCH_API_URL") or config_parser.get(
        "search", "api_url", fallback=""
    )

    return {"api_key": api_key, "api_url": api_url}


def get_video_config():
    config_parser = _read_config()

    api_key = os.getenv("VIDEO_API_KEY") or config_parser.get(
        "video", "api_key", fallback=""
    )

    return {"api_key": api_key}


def get_gf_config():
    config_parser = _read_config()

    api_key = os.getenv("GF_API_KEY") or config_parser.get(
        "gf", "api_key", fallback=""
    )

    return {"api_key": api_key}


def get_pdf_config():
    config_parser = _read_config()

    api_key = os.getenv("PDF_API_KEY") or config_parser.get(
        "pdf", "api_key", fallback=""
    )

    return {"api_key": api_key}


# ========== 按用途获取模型配置（新架构） ==========

# 数据目录路径
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "data", "web")


def _load_ai_models_from_file():
    """从文件加载AI模型配置列表"""
    import json
    ai_models_path = os.path.join(DATA_DIR, "ai_models.json")
    if os.path.exists(ai_models_path):
        try:
            with open(ai_models_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                # 返回模型列表，如果是字典格式则取"models"字段
                if isinstance(data, dict):
                    return data.get("models", [])
                elif isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def get_model_config_by_purpose(purpose: str) -> dict:
    """根据用途获取对应的活跃模型配置
    
    Args:
        purpose: 模型用途 (chat, vision, video, tts, stt, embedding)
    
    Returns:
        模型配置字典，如果没有找到则返回None
    """
    ai_models = _load_ai_models_from_file()
    
    for model in ai_models:
        if model.get("purpose", "chat") == purpose and model.get("enabled", True):
            config = {
                "api_key": resolve_runtime_api_key(
                    model.get("api_key", ""),
                    model.get("provider_type", "openai_compatible")
                ),
                "base_url": model.get("base_url", ""),
                "model": model.get("model", ""),
                "provider_type": model.get("provider_type", "openai_compatible"),
                "provider": model.get("provider", "custom"),
                "temperature": model.get("temperature", 0.7),
                "max_tokens": model.get("max_tokens", 2000),
                "top_p": model.get("top_p", 0.9),
                "system_prompt": model.get("system_prompt", ""),
                "supports_tools": model.get("supports_tools", True),
                "supports_reasoning": model.get("supports_reasoning", True),
                "supports_stream": model.get("supports_stream", True),
            }
            
            # 根据用途添加特有配置
            if purpose == "tts":
                config.update({
                    "voice": model.get("voice", "default"),
                    "speed": model.get("speed", 1.0),
                    "pitch": model.get("pitch", 1.0),
                    "volume": model.get("volume", 1.0),
                })
            elif purpose == "stt":
                config.update({
                    "language": model.get("language", "zh"),
                })
            elif purpose == "embedding":
                config.update({
                    "dimensions": model.get("dimensions", 1536),
                })
            
            return config
    
    return None


def get_chat_model_config() -> dict:
    """获取对话模型配置"""
    config = get_model_config_by_purpose("chat")
    if config:
        return config
    # 回退到传统配置
    return get_api_config()


def get_vision_model_config() -> dict:
    """获取图片理解模型配置"""
    config = get_model_config_by_purpose("vision")
    if config:
        return config
    # 回退到pic配置
    pic_config = get_pic_config()
    api_config = get_api_config()
    return {
        "api_key": api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": pic_config.get("model", "zai-org/GLM-4.6V"),
        "provider_type": api_config.get("provider_type", "openai_compatible"),
    }


def get_video_model_config() -> dict:
    """获取视频理解模型配置"""
    config = get_model_config_by_purpose("video")
    if config:
        return config
    # 回退到video配置
    video_config = get_video_config()
    api_config = get_api_config()
    return {
        "api_key": video_config.get("api_key") or api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": api_config.get("model", ""),
        "provider_type": api_config.get("provider_type", "openai_compatible"),
    }


def get_tts_model_config() -> dict:
    """获取TTS语音合成模型配置"""
    config = get_model_config_by_purpose("tts")
    if config:
        return config
    # 回退到voice配置
    voice_config = get_voice_config()
    api_config = get_api_config()
    voice = voice_config.get("voice", "fnlp/MOSS-TTSD-v0.5:diana")
    voice_parts = voice.split(":") if ":" in voice else [voice, "default"]
    return {
        "api_key": api_config.get("silicon_api_key") or api_config.get("api_key", ""),
        "base_url": "https://api.siliconflow.cn/v1",
        "model": voice_parts[0],
        "voice": voice_parts[1] if len(voice_parts) > 1 else "default",
        "provider_type": "siliconflow",
    }


def get_stt_model_config() -> dict:
    """获取STT语音识别模型配置"""
    config = get_model_config_by_purpose("stt")
    if config:
        return config
    # 默认配置
    api_config = get_api_config()
    return {
        "api_key": api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": "tiny",
        "language": "zh",
        "provider_type": api_config.get("provider_type", "openai_compatible"),
    }


def get_embedding_model_config() -> dict:
    """获取向量嵌入模型配置"""
    config = get_model_config_by_purpose("embedding")
    if config:
        return config
    # 回退到api_config中的embedding配置
    api_config = get_api_config()
    return {
        "api_key": api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": "text-embedding-3-small",
        "dimensions": 1536,
        "provider_type": api_config.get("provider_type", "openai_compatible"),
    }
