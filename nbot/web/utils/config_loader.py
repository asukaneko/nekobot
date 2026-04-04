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
