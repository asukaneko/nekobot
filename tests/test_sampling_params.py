"""采样参数接线测试：归一化、extra_body 合并与协议方言翻译。"""

from nbot.core.protocols.anthropic_messages import AnthropicMessagesProtocol
from nbot.core.protocols.gemini_native import GeminiNativeProtocol
from nbot.core.protocols.openai_chat import OpenAIChatProtocol
from nbot.core.protocols.openai_responses import OpenAIResponsesProtocol
from nbot.core.sampling import (
    is_reasoning_model,
    normalize_max_tokens,
    normalize_temperature,
    normalize_token_limit_field,
    normalize_top_p,
    request_extra_body,
    resolve_token_limit,
    sampling_extra_body,
    token_limit_field,
)
from nbot.services.ai import AIClient

MESSAGES = [{"role": "user", "content": "hello"}]
CONFIG = {"temperature": 0.8, "top_p": 0.95, "max_tokens": 4096}


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_temperature_clamps_and_rejects():
    assert normalize_temperature(0.7) == 0.7
    assert normalize_temperature("0.35") == 0.35
    assert normalize_temperature(3) == 2.0
    assert normalize_temperature(-1) == 0.0
    assert normalize_temperature(True) is None
    assert normalize_temperature("null") is None
    assert normalize_temperature(None) is None
    assert normalize_temperature(float("nan")) is None


def test_normalize_top_p_range():
    assert normalize_top_p(0.9) == 0.9
    assert normalize_top_p(1.5) == 1.0
    assert normalize_top_p(0) is None
    assert normalize_top_p(-0.2) is None
    assert normalize_top_p("") is None


def test_normalize_max_tokens_positive_only():
    assert normalize_max_tokens(2000) == 2000
    assert normalize_max_tokens("8192") == 8192
    assert normalize_max_tokens(0) is None
    assert normalize_max_tokens(-10) is None
    assert normalize_max_tokens("abc") is None
    assert normalize_max_tokens(10**9) == 2_000_000


def test_resolve_token_limit_prefers_config():
    assert resolve_token_limit({"max_tokens": 512}, 4096) == 512
    assert resolve_token_limit({}, 4096) == 4096
    assert resolve_token_limit(None) is None
    assert resolve_token_limit({"max_tokens": 0}) is None


def test_normalize_token_limit_field():
    assert normalize_token_limit_field("MAX_TOKENS") == "max_tokens"
    assert normalize_token_limit_field("max_completion_tokens") == "max_completion_tokens"
    assert normalize_token_limit_field("auto") == ""


def test_reasoning_model_detection_and_field():
    assert is_reasoning_model("gpt-5.5")
    assert is_reasoning_model("openai/gpt-5")
    assert is_reasoning_model("o3-mini")
    assert not is_reasoning_model("deepseek-chat")
    assert not is_reasoning_model("openai")
    assert not is_reasoning_model("")
    assert token_limit_field("gpt-5") == "max_completion_tokens"
    assert token_limit_field("deepseek-chat") == "max_tokens"
    assert token_limit_field("deepseek-chat", {"token_limit_field": "max_completion_tokens"}) == (
        "max_completion_tokens"
    )


# ---------------------------------------------------------------------------
# extra_body 合并
# ---------------------------------------------------------------------------


def test_sampling_extra_body_partial_config():
    assert sampling_extra_body(None) == {}
    assert sampling_extra_body({}) == {}
    assert sampling_extra_body({"temperature": 0.2}) == {"temperature": 0.2}
    assert sampling_extra_body({"top_p": 0, "max_tokens": ""}) == {}


def test_request_extra_body_merges_stop_and_sampling():
    extra = request_extra_body(
        {
            "stop": "User:\nAssistant:",
            "prompt_caching": True,
            "temperature": 0.6,
            "top_p": 0.8,
            "max_tokens": 1500,
        }
    )
    assert extra["stop"] == ["User:", "Assistant:"]
    assert extra["prompt_caching"] is True
    assert extra["temperature"] == 0.6
    assert extra["top_p"] == 0.8
    assert extra["max_tokens"] == 1500
    assert request_extra_body({}) == {}


# ---------------------------------------------------------------------------
# OpenAI Chat Completions
# ---------------------------------------------------------------------------


def test_openai_chat_payload_includes_sampling_params():
    payload = OpenAIChatProtocol().build_payload(
        "deepseek-chat",
        MESSAGES,
        extra_body=request_extra_body(CONFIG),
    )
    assert payload["temperature"] == 0.8
    assert payload["top_p"] == 0.95
    assert payload["max_tokens"] == 4096
    assert "max_completion_tokens" not in payload


def test_openai_chat_omits_unconfigured_sampling_params():
    payload = OpenAIChatProtocol().build_payload("deepseek-chat", MESSAGES)
    for key in ("temperature", "top_p", "max_tokens", "max_completion_tokens"):
        assert key not in payload


def test_openai_chat_reasoning_model_switches_token_field():
    payload = OpenAIChatProtocol().build_payload(
        "gpt-5.5",
        MESSAGES,
        extra_body=request_extra_body(CONFIG),
    )
    assert payload["max_completion_tokens"] == 4096
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload


def test_openai_chat_explicit_token_limit_field_wins():
    payload = OpenAIChatProtocol().build_payload(
        "gpt-5.5",
        MESSAGES,
        extra_body=request_extra_body(
            {"max_tokens": 2048, "token_limit_field": "max_tokens"}
        ),
    )
    assert payload["max_tokens"] == 2048
    assert "max_completion_tokens" not in payload


def test_openai_chat_explicit_max_tokens_argument():
    payload = OpenAIChatProtocol().build_payload("deepseek-chat", MESSAGES, max_tokens=64)
    assert payload["max_tokens"] == 64


def test_openai_chat_does_not_leak_internal_keys():
    payload = OpenAIChatProtocol().build_payload(
        "deepseek-chat",
        MESSAGES,
        extra_body={"prompt_caching": True, "max_tokens": 100, "stream_options": {"x": 1}},
    )
    assert "prompt_caching" not in payload
    assert payload["stream_options"] == {"x": 1}
    assert payload["max_tokens"] == 100


# ---------------------------------------------------------------------------
# Anthropic
# ---------------------------------------------------------------------------


def test_anthropic_clamps_temperature_and_uses_config_tokens():
    payload = AnthropicMessagesProtocol().build_payload(
        "claude-test",
        MESSAGES,
        extra_body=request_extra_body({"temperature": 1.8, "top_p": 0.7, "max_tokens": 3000}),
    )
    assert payload["temperature"] == 1.0
    assert payload["top_p"] == 0.7
    assert payload["max_tokens"] == 3000


def test_anthropic_default_token_limit_when_unconfigured():
    payload = AnthropicMessagesProtocol().build_payload("claude-test", MESSAGES)
    assert payload["max_tokens"] == 4096
    assert "temperature" not in payload
    assert "top_p" not in payload


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def test_gemini_maps_sampling_params_to_generation_config():
    payload = GeminiNativeProtocol().build_payload(
        "gemini-test",
        MESSAGES,
        extra_body=request_extra_body({"temperature": 1.4, "top_p": 0.6, "max_tokens": 2048}),
    )
    gen = payload["generationConfig"]
    assert gen["temperature"] == 1.4
    assert gen["topP"] == 0.6
    assert gen["maxOutputTokens"] == 2048
    assert "top_p" not in gen


def test_gemini_accepts_camel_case_override():
    payload = GeminiNativeProtocol().build_payload(
        "gemini-test",
        MESSAGES,
        extra_body={"topP": 0.4, "topK": 32},
    )
    gen = payload["generationConfig"]
    assert gen["topP"] == 0.4
    assert gen["topK"] == 32
    assert gen["maxOutputTokens"] == 4096


# ---------------------------------------------------------------------------
# OpenAI Responses
# ---------------------------------------------------------------------------


def test_responses_maps_sampling_params():
    payload = OpenAIResponsesProtocol().build_payload(
        "gpt-test",
        MESSAGES,
        extra_body=request_extra_body({"temperature": 0.3, "top_p": 0.5, "max_tokens": 1024}),
    )
    assert payload["temperature"] == 0.3
    assert payload["top_p"] == 0.5
    assert payload["max_output_tokens"] == 1024
    assert "max_tokens" not in payload


def test_responses_reasoning_model_drops_temperature():
    payload = OpenAIResponsesProtocol().build_payload(
        "gpt-5",
        MESSAGES,
        extra_body=request_extra_body({"temperature": 0.3, "max_tokens": 2048}),
    )
    assert "temperature" not in payload
    assert payload["max_output_tokens"] == 2048


# ---------------------------------------------------------------------------
# AIClient 参数快照
# ---------------------------------------------------------------------------


def _client():
    return AIClient(
        api_key="k",
        base_url="https://example.test/v1",
        model="deepseek-chat",
        pic_model="",
        search_api_key="",
        search_api_url="",
        video_api="",
    )


def test_ai_client_sampling_config_defaults():
    assert _client().sampling_config() == {
        "stop": "",
        "temperature": None,
        "top_p": None,
        "max_tokens": None,
        "token_limit_field": "",
    }


def test_ai_client_sampling_config_round_trip():
    client = _client()
    client.stop = "User:"
    client.temperature = 0.9
    client.top_p = 0.88
    client.max_tokens = 3200
    client.token_limit_field = "max_completion_tokens"
    extra = request_extra_body(client.sampling_config())
    assert extra == {
        "stop": ["User:"],
        "temperature": 0.9,
        "top_p": 0.88,
        "max_tokens": 3200,
        "token_limit_field": "max_completion_tokens",
    }
