"""模型能力实测（probe）测试"""

import base64

import pytest

from nbot.web import model_probe as mp


class _Resp:
    def __init__(self, status=200, data=None, text=""):
        self.status_code = status
        self._data = data if data is not None else {}
        self.text = text or ""

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


def _config(**over):
    cfg = {
        "base_url": "https://api.example.com/v1",
        "model": "test-model",
        "api_key": "sk-test",
        "provider_type": "openai_compatible",
    }
    cfg.update(over)
    return cfg


def test_tiny_png_is_valid_png():
    raw = base64.b64decode(mp._TINY_PNG)
    assert raw.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"IHDR" in raw and b"IEND" in raw


def test_unsupported_protocol_reports_clearly():
    result = mp.probe_model_config(_config(provider_type="anthropic_messages"))
    assert result["supported"] is False
    assert "暂不支持" in result["error"]


def test_missing_base_url():
    result = mp.probe_model_config(_config(base_url=""))
    assert result["supported"] is False
    assert "base_url" in result["error"]


def test_all_capabilities_true(monkeypatch):
    calls = []

    def fake_post(url, headers, payload, timeout):
        calls.append(payload)
        if payload.get("stream"):
            return _Resp(200, {})
        if "tools" in payload:
            return _Resp(200, {"choices": [{"message": {"tool_calls": [{"id": "t1"}]}}]})
        message = {"role": "assistant", "content": [{"type": "image_url", "url": "x"}] if "image_url" in str(payload) else "pong"}
        return _Resp(200, {"choices": [{"message": message}]})

    monkeypatch.setattr(mp, "_post", fake_post)
    result = mp.probe_model_config(_config())

    assert result["supported"] is True
    assert result["ok"] is True
    assert result["capabilities"] == {"chat": True, "tools": True, "vision": True, "stream": True}
    assert result["latency_ms"] >= 0
    assert len(calls) == 4


def test_chat_failure_reports_error(monkeypatch):
    def fake_post(url, headers, payload, timeout):
        return _Resp(401, {"error": {"message": "invalid api key"}})

    monkeypatch.setattr(mp, "_post", fake_post)
    result = mp.probe_model_config(_config(), checks=["chat"])

    assert result["ok"] is False
    assert "invalid api key" in result["error"]
    assert result["capabilities"]["chat"] is False


def test_tools_detected_via_finish_reason(monkeypatch):
    def fake_post(url, headers, payload, timeout):
        if "tools" in payload:
            return _Resp(200, {"choices": [{"message": {"content": ""}, "finish_reason": "tool_calls"}]})
        return _Resp(200, {"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(mp, "_post", fake_post)
    result = mp.probe_model_config(_config(), checks=["chat", "tools"])
    assert result["capabilities"]["tools"] is True


def test_vision_rejected(monkeypatch):
    def fake_post(url, headers, payload, timeout):
        if "image_url" in str(payload):
            return _Resp(400, {"error": {"message": "model does not support images"}})
        return _Resp(200, {"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(mp, "_post", fake_post)
    result = mp.probe_model_config(_config(), checks=["chat", "vision"])
    assert result["capabilities"]["vision"] is False
    assert "images" in result["details"]["vision"]["error"]


def test_exception_is_captured(monkeypatch):
    def boom(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(mp, "_post", boom)
    result = mp.probe_model_config(_config(), checks=["chat"])
    assert result["capabilities"]["chat"] is False
    assert "timed out" in result["details"]["chat"]["error"]


def test_apply_probe_result_writes_capabilities():
    config = {"id": "m1", "supports_tools": True, "supports_stream": True}
    result = {
        "supported": True,
        "ok": True,
        "latency_ms": 321,
        "capabilities": {"tools": False, "stream": True, "vision": True},
    }
    updated = mp.apply_probe_result(config, result)
    assert updated["supports_tools"] is False
    assert updated["supports_stream"] is True
    assert updated["supports_vision"] is True
    assert updated["capabilities_probed"] is True
    assert updated["capabilities_probe_latency_ms"] == 321
    assert updated["capabilities_probed_at"]


def test_apply_probe_result_ignores_unsupported():
    config = {"id": "m1", "supports_tools": True}
    mp.apply_probe_result(config, {"supported": False, "capabilities": {"tools": False}})
    assert config["supports_tools"] is True
    assert "capabilities_probed" not in config


def test_reasoning_model_probe_skips_token_cap(monkeypatch):
    calls = []

    def fake_post(url, headers, payload, timeout):
        calls.append(payload)
        return _Resp(200, {"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(mp, "_post", fake_post)
    mp.probe_model_config(_config(model="gpt-5.5"), checks=["chat"])
    assert "max_completion_tokens" not in calls[0]
    assert "max_tokens" not in calls[0]


def test_plain_model_probe_keeps_small_budget(monkeypatch):
    calls = []

    def fake_post(url, headers, payload, timeout):
        calls.append(payload)
        return _Resp(200, {"choices": [{"message": {"content": "pong"}}]})

    monkeypatch.setattr(mp, "_post", fake_post)
    mp.probe_model_config(_config(model="deepseek-chat"), checks=["chat"])
    assert calls[0]["max_tokens"] == 16
