"""模型采样参数工具：停止字符串、温度、top_p 与输出上限的归一化与请求注入。

模型配置中的 stop 字段允许字符串（按行分隔）或字符串列表，
协议层会按各家 API 翻译为 stop / stop_sequences / stopSequences。

采样参数在配置里是可选的：只有显式配置过的值才会进入请求体，
未配置时保留厂商默认值，避免“默认值覆盖”导致的输出行为突变。

各协议方言（见 nbot/core/protocols/）：
- OpenAI Chat Completions：temperature / top_p / max_tokens；
  推理系模型（gpt-5、o1、o3、o4、codex）改用 max_completion_tokens，
  并忽略 temperature / top_p（官方只接受默认温度）。
- OpenAI Responses：temperature / top_p / max_output_tokens。
- Anthropic Messages：temperature（0~1）/ top_p / top_k / max_tokens（必填）。
- Gemini：generationConfig.temperature / topP / topK / maxOutputTokens。
"""

import math
from typing import Any, Dict, List, Optional

MAX_STOP_SEQUENCES = 4
MAX_STOP_LENGTH = 64

# 各用途新建模型的默认输出上限（前后端与配置加载共用同一套数值）
DEFAULT_CHAT_MAX_TOKENS = 8192
DEFAULT_VISION_MAX_TOKENS = 1000
DEFAULT_VIDEO_MAX_TOKENS = 1500
DEFAULT_PURPOSE_MAX_TOKENS = {
    "chat": DEFAULT_CHAT_MAX_TOKENS,
    "vision": DEFAULT_VISION_MAX_TOKENS,
    "video": DEFAULT_VIDEO_MAX_TOKENS,
}


def default_max_tokens_for(purpose: str = "chat") -> int:
    """按用途返回默认输出上限；未知用途按对话用途处理。"""
    return DEFAULT_PURPOSE_MAX_TOKENS.get(str(purpose or "chat"), DEFAULT_CHAT_MAX_TOKENS)

# 温度上限：OpenAI / Gemini 允许 0~2，Anthropic 只允许 0~1（协议层再收紧）
MAX_TEMPERATURE = 2.0
# top_p 合法区间为 (0, 1]，0 视为未配置
MAX_TOP_P = 1.0
MAX_OUTPUT_TOKENS = 2_000_000

# OpenAI 推理系模型前缀：只接受默认温度，输出上限字段也不同
REASONING_MODEL_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt5", "codex")

# 允许显式指定的输出上限字段
TOKEN_LIMIT_FIELDS = ("max_tokens", "max_completion_tokens")


def normalize_stop_strings(value: Any) -> List[str]:
    """把模型配置里的停止字符串归一化为字符串列表。"""
    if not value:
        return []

    candidates: List[str] = []
    if isinstance(value, str):
        candidates = _split_lines(value)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                candidates.extend(_split_lines(item))

    stops: List[str] = []
    for candidate in candidates:
        text = candidate.strip()
        if not text or text in stops:
            continue
        stops.append(text[:MAX_STOP_LENGTH])
        if len(stops) >= MAX_STOP_SEQUENCES:
            break
    return stops


def normalize_temperature(value: Any) -> Optional[float]:
    """把温度归一化到 0~2，非法或未配置返回 None。"""
    number = _to_float(value)
    if number is None:
        return None
    return round(min(max(number, 0.0), MAX_TEMPERATURE), 3)


def normalize_top_p(value: Any) -> Optional[float]:
    """把 top_p 归一化到 (0, 1]，非法或未配置返回 None。"""
    number = _to_float(value)
    if number is None or number <= 0:
        return None
    return round(min(number, MAX_TOP_P), 4)


def normalize_max_tokens(value: Any) -> Optional[int]:
    """把输出上限归一化为正整数，非法或未配置返回 None。"""
    number = _to_float(value)
    if number is None or number <= 0:
        return None
    return min(int(number), MAX_OUTPUT_TOKENS)


def normalize_token_limit_field(value: Any) -> str:
    """归一化输出上限字段名，未知值返回空串（表示自动判断）。"""
    field = str(value or "").strip().lower()
    return field if field in TOKEN_LIMIT_FIELDS else ""


def is_reasoning_model(model: str) -> bool:
    """判断模型是否属于只接受默认温度的 OpenAI 推理系。"""
    name = str(model or "").strip().lower()
    if not name:
        return False
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return any(name.startswith(prefix) for prefix in REASONING_MODEL_PREFIXES)


def token_limit_field(model: str, config: Any = None) -> str:
    """决定输出上限字段名：显式配置优先，否则按模型族自动判断。"""
    explicit = normalize_token_limit_field(
        config.get("token_limit_field") if isinstance(config, dict) else None
    )
    if explicit:
        return explicit
    return "max_completion_tokens" if is_reasoning_model(model) else "max_tokens"


def resolve_token_limit(config: Any, default: Optional[int] = None) -> Optional[int]:
    """从配置读取输出上限，未配置时回退到 default。"""
    configured = normalize_max_tokens(
        config.get("max_tokens") if isinstance(config, dict) else None
    )
    return configured if configured is not None else default


def sampling_extra_body(config: Any) -> Dict[str, Any]:
    """从模型配置中提取采样参数，包装为协议层 extra_body。"""
    if not isinstance(config, dict):
        return {}
    extra: Dict[str, Any] = {}
    temperature = normalize_temperature(config.get("temperature"))
    if temperature is not None:
        extra["temperature"] = temperature
    top_p = normalize_top_p(config.get("top_p"))
    if top_p is not None:
        extra["top_p"] = top_p
    max_tokens = normalize_max_tokens(config.get("max_tokens"))
    if max_tokens is not None:
        extra["max_tokens"] = max_tokens
    field = normalize_token_limit_field(config.get("token_limit_field"))
    if field:
        extra["token_limit_field"] = field
    return extra


def stop_extra_body(config: Any) -> Dict[str, Any]:
    """从模型配置中提取 stop 与 prompt_caching，包装为协议层 extra_body。"""
    if not isinstance(config, dict):
        return {}
    extra: Dict[str, Any] = {}
    stops = normalize_stop_strings(config.get("stop"))
    if stops:
        extra["stop"] = stops
    if config.get("prompt_caching"):
        extra["prompt_caching"] = True
    return extra


def request_extra_body(config: Any) -> Dict[str, Any]:
    """合并停止字符串与采样参数，生成完整 extra_body。"""
    extra = stop_extra_body(config)
    extra.update(sampling_extra_body(config))
    return extra


def _to_float(value: Any) -> Optional[float]:
    """宽松解析数字，拒绝布尔值、NaN 与无穷大。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text or text.lower() in ("null", "none", "auto"):
            return None
        value = text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _split_lines(text: str) -> List[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
