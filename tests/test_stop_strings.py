from nbot.core.protocols.anthropic_messages import AnthropicMessagesProtocol
from nbot.core.protocols.gemini_native import GeminiNativeProtocol
from nbot.core.protocols.openai_chat import OpenAIChatProtocol
from nbot.core.protocols.openai_responses import OpenAIResponsesProtocol
from nbot.core.sampling import normalize_stop_strings, stop_extra_body

MESSAGES = [{"role": "user", "content": "hello"}]


def test_normalize_stop_strings_from_multiline_text():
    value = "User:\n\nAssistant:\r\n  END  \nUser:"
    assert normalize_stop_strings(value) == ["User:", "Assistant:", "END"]


def test_normalize_stop_strings_caps_count_and_length():
    value = ["a", "b", "c", "d", "e"]
    assert normalize_stop_strings(value) == ["a", "b", "c", "d"]
    assert normalize_stop_strings("x" * 200)[0] == "x" * 64


def test_stop_extra_body_empty_values():
    assert stop_extra_body({}) == {}
    assert stop_extra_body({"stop": ""}) == {}
    assert stop_extra_body(None) == {}


def test_openai_chat_payload_uses_stop():
    payload = OpenAIChatProtocol().build_payload(
        "gpt-test",
        MESSAGES,
        extra_body={"stop": ["User:"], "stream_options": {"include_usage": True}},
    )
    assert payload["stop"] == ["User:"]
    assert payload["stream_options"] == {"include_usage": True}


def test_anthropic_payload_uses_stop_sequences():
    payload = AnthropicMessagesProtocol().build_payload(
        "claude-test",
        MESSAGES,
        extra_body={"stop": ["User:"]},
    )
    assert payload["stop_sequences"] == ["User:"]
    assert "stop" not in payload


def test_gemini_payload_uses_generation_config_stop_sequences():
    payload = GeminiNativeProtocol().build_payload(
        "gemini-test",
        MESSAGES,
        extra_body={"stop": ["User:"]},
    )
    assert payload["generationConfig"]["stopSequences"] == ["User:"]


def test_openai_responses_payload_ignores_stop():
    payload = OpenAIResponsesProtocol().build_payload(
        "responses-test",
        MESSAGES,
        extra_body={"stop": ["User:"], "temperature": 0.5},
    )
    assert "stop" not in payload
    assert payload["temperature"] == 0.5
