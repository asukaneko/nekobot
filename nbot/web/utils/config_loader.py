import configparser
import os

from dotenv import load_dotenv

from nbot.core.sampling import default_max_tokens_for
from nbot.web.secure_store import read_secure_json, write_secure_json

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


def _env_bool(name: str):
    value = os.getenv(name)
    if value is None:
        return None
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def resolve_runtime_api_key(configured_api_key: str = "", provider_type: str = "") -> str:
    provider = (provider_type or "").strip().lower()
    if provider == "minimax":
        return os.getenv("MINIMAX_API_KEY") or os.getenv("API_KEY") or configured_api_key
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
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
        "provider_type": provider_type,
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


def get_stt_local_config() -> dict:
    config_parser = _read_config()
    env_enabled = _env_bool("NBOT_LOCAL_STT_ENABLED")
    local_enabled = (
        env_enabled
        if env_enabled is not None
        else config_parser.getboolean("stt", "local_enabled", fallback=False)
    )

    return {
        "local_enabled": local_enabled,
        "model": os.getenv("NBOT_FASTER_WHISPER_MODEL")
        or config_parser.get("stt", "model", fallback="tiny"),
        "language": os.getenv("NBOT_STT_LANGUAGE")
        or config_parser.get("stt", "language", fallback="zh"),
        "device": os.getenv("NBOT_FASTER_WHISPER_DEVICE")
        or config_parser.get("stt", "device", fallback="cpu"),
        "compute_type": os.getenv("NBOT_FASTER_WHISPER_COMPUTE_TYPE")
        or config_parser.get("stt", "compute_type", fallback="int8"),
        "beam_size": os.getenv("NBOT_FASTER_WHISPER_BEAM_SIZE")
        or config_parser.get("stt", "beam_size", fallback="5"),
    }


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


# ========== 鎸夌敤閫旇幏鍙栨ā鍨嬮厤缃紙鏂版灦鏋勶級 ==========

# 鏁版嵁鐩綍璺緞
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "data", "web")


def _load_ai_models_from_file():
    """浠庢枃浠跺姞杞紸I妯″瀷閰嶇疆鍒楄〃"""
    ai_models_path = os.path.join(DATA_DIR, "ai_models.json")
    if os.path.exists(ai_models_path):
        try:
            data, was_plaintext = read_secure_json(ai_models_path, DATA_DIR, {})
            if was_plaintext:
                write_secure_json(ai_models_path, DATA_DIR, data)
            if isinstance(data, dict):
                return data.get("models", [])
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def get_model_config_by_purpose(purpose: str) -> dict:
    """鏍规嵁鐢ㄩ€旇幏鍙栧搴旂殑娲昏穬妯″瀷閰嶇疆
    
    Args:
        purpose: 妯″瀷鐢ㄩ€?(chat, vision, video, tts, stt, embedding)
    
    Returns:
        妯″瀷閰嶇疆瀛楀吀锛屽鏋滄病鏈夋壘鍒板垯杩斿洖None
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
                "proxy_url": model.get("proxy_url", ""),
                "model": model.get("model", ""),
                "provider_type": model.get("provider_type", "openai_compatible"),
                "provider": model.get("provider", "custom"),
                "append_base_url_path": model.get("append_base_url_path", True),
                "temperature": model.get("temperature", 0.7),
                "max_tokens": model.get("max_tokens", default_max_tokens_for(purpose)),
                "max_context_length": model.get("max_context_length", 100000),
                "top_p": model.get("top_p", 0.9),
                "token_limit_field": model.get("token_limit_field", ""),
                "stop": model.get("stop", ""),
                "system_prompt": model.get("system_prompt", ""),
                "supports_tools": model.get("supports_tools", True),
                "supports_reasoning": model.get("supports_reasoning", True),
                "supports_stream": model.get("supports_stream", True),
            }
            
            # Add purpose-specific config.
            if purpose == "tts":
                config.update({
                    "voice": model.get("voice", "default"),
                    "speed": model.get("speed", 1.0),
                    "pitch": model.get("pitch", 1.0),
                    "volume": model.get("volume", 1.0),
                    "tts_provider": model.get("tts_provider", "openai"),
                    "tts_url": model.get("tts_url", ""),
                    "tts_model": model.get("tts_model", ""),
                    "tts_voice": model.get("tts_voice", model.get("voice", "default")),
                    "tts_speed": model.get("tts_speed", model.get("speed", 1.0)),
                    "tts_pitch": model.get("tts_pitch", model.get("pitch", 1.0)),
                    "tts_volume": model.get("tts_volume", model.get("volume", 1.0)),
                    "tts_format": model.get("tts_format", "mp3"),
                    "tts_upload_url": model.get("tts_upload_url", ""),
                    "tts_headers": model.get("tts_headers", ""),
                    "tts_body_template": model.get("tts_body_template", ""),
                })
            elif purpose == "stt":
                config.update({
                    "language": model.get("language", "zh"),
                    "stt_provider": model.get("stt_provider", ""),
                    "stt_url": model.get("stt_url", ""),
                    "stt_model": model.get("stt_model", ""),
                    "stt_language": model.get("stt_language", model.get("language", "zh")),
                    "stt_headers": model.get("stt_headers", ""),
                })
            elif purpose == "embedding":
                config.update({
                    "dimensions": model.get("dimensions", 1536),
                })
            
            return config
    
    return None


def get_model_configs_by_purpose(purpose: str) -> list:
    """Return all enabled model configs for a purpose, ordered by priority.

    Lower priority value = higher priority (tried first).
    Models with the same priority preserve their original order
    in ai_models.json. Used by the failover queue.

    Args:
        purpose: Model purpose (chat, vision, video, tts, stt, embedding)

    Returns:
        List of model config dicts, sorted by priority ascending.
        Each dict includes 'model_id' and 'priority' keys.
        Empty list if no enabled models found for the purpose.
    """
    ai_models = _load_ai_models_from_file()

    matches = []
    for idx, model in enumerate(ai_models):
        if model.get("purpose", "chat") != purpose:
            continue
        if not model.get("enabled", True):
            continue

        config = {
            "model_id": model.get("id", ""),
            "priority": model.get("priority", 0),
            "api_key": resolve_runtime_api_key(
                model.get("api_key", ""),
                model.get("provider_type", "openai_compatible"),
            ),
            "base_url": model.get("base_url", ""),
            "proxy_url": model.get("proxy_url", ""),
            "model": model.get("model", ""),
            "provider_type": model.get("provider_type", "openai_compatible"),
            "provider": model.get("provider", "custom"),
            "append_base_url_path": model.get("append_base_url_path", True),
            "temperature": model.get("temperature", 0.7),
            "max_tokens": model.get("max_tokens", default_max_tokens_for(purpose)),
            "max_context_length": model.get("max_context_length", 100000),
            "top_p": model.get("top_p", 0.9),
            "token_limit_field": model.get("token_limit_field", ""),
            "stop": model.get("stop", ""),
            "system_prompt": model.get("system_prompt", ""),
            "supports_tools": model.get("supports_tools", True),
            "supports_reasoning": model.get("supports_reasoning", True),
            "supports_stream": model.get("supports_stream", True),
            "token_limit_daily": model.get("token_limit_daily", 0) or 0,
            "token_limit_weekly": model.get("token_limit_weekly", 0) or 0,
            "failover_timeout": model.get("failover_timeout", 0) or 0,
            "name": model.get("name", ""),
            "_order": idx,
        }

        # Add purpose-specific config
        if purpose == "tts":
            config.update({
                "voice": model.get("voice", "default"),
                "speed": model.get("speed", 1.0),
                "pitch": model.get("pitch", 1.0),
                "volume": model.get("volume", 1.0),
                "tts_provider": model.get("tts_provider", "openai"),
                "tts_url": model.get("tts_url", ""),
                "tts_model": model.get("tts_model", ""),
                "tts_voice": model.get("tts_voice", model.get("voice", "default")),
                "tts_speed": model.get("tts_speed", model.get("speed", 1.0)),
                "tts_pitch": model.get("tts_pitch", model.get("pitch", 1.0)),
                "tts_volume": model.get("tts_volume", model.get("volume", 1.0)),
                "tts_format": model.get("tts_format", "mp3"),
                "tts_upload_url": model.get("tts_upload_url", ""),
                "tts_headers": model.get("tts_headers", ""),
                "tts_body_template": model.get("tts_body_template", ""),
            })
        elif purpose == "stt":
            config.update({
                "language": model.get("language", "zh"),
            })
        elif purpose == "embedding":
            config.update({
                "dimensions": model.get("dimensions", 1536),
            })

        matches.append(config)

    # Sort by priority ascending, then by original order
    matches.sort(key=lambda x: (x.get("priority", 0), x.get("_order", 0)))
    # Remove internal sort key
    for m in matches:
        m.pop("_order", None)

    return matches


def get_chat_model_config() -> dict:
    """鑾峰彇瀵硅瘽妯″瀷閰嶇疆"""
    config = get_model_config_by_purpose("chat")
    if config:
        return config
    # 鍥為€€鍒颁紶缁熼厤缃?    return get_api_config()


def get_vision_model_config() -> dict:
    """鑾峰彇鍥剧墖鐞嗚В妯″瀷閰嶇疆"""
    config = get_model_config_by_purpose("vision")
    if config:
        return config
    # 鍥為€€鍒皃ic閰嶇疆
    pic_config = get_pic_config()
    api_config = get_api_config()
    return {
        "api_key": api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": pic_config.get("model", "zai-org/GLM-4.6V"),
        "provider_type": api_config.get("provider_type", "openai_compatible"),
    }


def get_video_model_config() -> dict:
    """鑾峰彇瑙嗛鐞嗚В妯″瀷閰嶇疆"""
    config = get_model_config_by_purpose("video")
    if config:
        return config
    # 鍥為€€鍒皏ideo閰嶇疆
    video_config = get_video_config()
    api_config = get_api_config()
    return {
        "api_key": video_config.get("api_key") or api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": api_config.get("model", ""),
        "provider_type": api_config.get("provider_type", "openai_compatible"),
    }


def get_tts_model_config() -> dict:
    """鑾峰彇TTS璇煶鍚堟垚妯″瀷閰嶇疆"""
    config = get_model_config_by_purpose("tts")
    if config:
        return config
    # 鍥為€€鍒皏oice閰嶇疆
    voice_config = get_voice_config()
    api_config = get_api_config()
    voice = voice_config.get("voice", "fnlp/MOSS-TTSD-v0.5:diana")
    voice_parts = voice.split(":") if ":" in voice else [voice, "default"]
    return {
        "api_key": api_config.get("api_key", ""),
        "base_url": "https://api.siliconflow.cn/v1",
        "model": voice_parts[0],
        "voice": voice_parts[1] if len(voice_parts) > 1 else "default",
        "provider_type": "siliconflow",
    }


def get_stt_model_config() -> dict:
    local_config = get_stt_local_config()
    """鑾峰彇STT璇煶璇嗗埆妯″瀷閰嶇疆"""
    config = get_model_config_by_purpose("stt")
    if config:
        for key, value in local_config.items():
            config.setdefault(key, value)
        return config
    # 榛樿閰嶇疆
    api_config = get_api_config()
    return {
        "api_key": api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": local_config["model"],
        "language": local_config["language"],
        "provider_type": api_config.get("provider_type", "openai_compatible"),
        "local_enabled": local_config["local_enabled"],
        "device": local_config["device"],
        "compute_type": local_config["compute_type"],
        "beam_size": local_config["beam_size"],
        "stt_provider": "",
        "stt_url": "",
        "stt_model": "",
        "stt_language": local_config["language"],
        "stt_headers": "",
    }


def get_embedding_model_config() -> dict:
    """鑾峰彇鍚戦噺宓屽叆妯″瀷閰嶇疆"""
    config = get_model_config_by_purpose("embedding")
    if config:
        return config
    # 鍥為€€鍒癮pi_config涓殑embedding閰嶇疆
    api_config = get_api_config()
    return {
        "api_key": api_config.get("api_key", ""),
        "base_url": api_config.get("base_url", ""),
        "model": "text-embedding-3-small",
        "dimensions": 1536,
        "provider_type": api_config.get("provider_type", "openai_compatible"),
    }


def get_character_runtime_config() -> dict:
    """获取角色运行时配置

    从 config.ini 的 [character_runtime] 和各频道配置中读取。

    Returns:
        配置字典，结构如下：
        {
            "character_runtime": {
                "default_enabled": bool,
                "default_character_id": str,
            },
            "channels": {
                "qq": {
                    "character_runtime": {
                        "enabled": bool,
                        "trigger": str,
                        "memory_scope": str,
                        "legacy_prompt_enabled": bool,
                    }
                },
                ...
            }
        }
    """
    config_parser = _read_config()

    # 全局配置
    default_enabled = config_parser.getboolean(
        "character_runtime", "default_enabled", fallback=True
    )
    default_character_id = config_parser.get(
        "character_runtime", "default_character_id", fallback=""
    )

    # 频道配置
    channels = {}

    # QQ 频道
    qq_enabled = config_parser.getboolean(
        "character_runtime_qq", "enabled", fallback=True
    )
    qq_trigger = config_parser.get(
        "character_runtime_qq", "trigger", fallback="mention_or_private"
    )
    qq_memory_scope = config_parser.get(
        "character_runtime_qq", "memory_scope", fallback="group_user"
    )
    qq_legacy_prompt_enabled = config_parser.getboolean(
        "character_runtime_qq", "legacy_prompt_enabled", fallback=False
    )
    channels["qq"] = {
        "character_runtime": {
            "enabled": qq_enabled,
            "trigger": qq_trigger,
            "memory_scope": qq_memory_scope,
            "legacy_prompt_enabled": qq_legacy_prompt_enabled,
        },
    }

    # 飞书频道
    feishu_enabled = config_parser.getboolean(
        "character_runtime_feishu", "enabled", fallback=True
    )
    feishu_trigger = config_parser.get(
        "character_runtime_feishu", "trigger", fallback="mention_or_private"
    )
    feishu_memory_scope = config_parser.get(
        "character_runtime_feishu", "memory_scope", fallback="chat_user"
    )
    channels["feishu"] = {
        "character_runtime": {
            "enabled": feishu_enabled,
            "trigger": feishu_trigger,
            "memory_scope": feishu_memory_scope,
        },
    }

    # Telegram 频道
    telegram_enabled = config_parser.getboolean(
        "character_runtime_telegram", "enabled", fallback=True
    )
    telegram_trigger = config_parser.get(
        "character_runtime_telegram", "trigger", fallback="private_or_reply"
    )
    telegram_memory_scope = config_parser.get(
        "character_runtime_telegram", "memory_scope", fallback="chat_user"
    )
    channels["telegram"] = {
        "character_runtime": {
            "enabled": telegram_enabled,
            "trigger": telegram_trigger,
            "memory_scope": telegram_memory_scope,
        },
    }

    # Web 频道（默认启用）
    channels["web"] = {
        "character_runtime": {
            "enabled": True,
        },
    }

    return {
        "character_runtime": {
            "default_enabled": default_enabled,
            "default_character_id": default_character_id,
        },
        "channels": channels,
    }
