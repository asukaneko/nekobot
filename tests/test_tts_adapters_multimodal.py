"""Tests for the new multimodal TTS adapters (Gemini / GLM / MiniMax / Qwen)."""
import base64
import json

import pytest

from nbot.services.tts_adapters.gemini_adapter import GeminiTTSAdapter
from nbot.services.tts_adapters.glm_adapter import GlmTTSAdapter
from nbot.services.tts_adapters.minimax_adapter import MiniMaxTTSAdapter
from nbot.services.tts_adapters.qwen_adapter import QwenTTSAdapter


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text="", payload=None):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        raise ValueError("not json")


def _patch_post(monkeypatch, handler):
    def fake_post(url, headers, data, timeout, **kwargs):
        return handler(url, headers, data)
    monkeypatch.setattr("requests.post", fake_post)


def test_gemini_tts_uses_default_url_and_decodes_inline_audio(tmp_path, monkeypatch):
    expected = b"RIFF" + b"\x00" * 100  # WAV-ish blob（Gemini 返回的已是 WAV）
    captured = {}

    def handler(url, headers, data):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(data)
        payload = {
            "candidates": [
                {"content": {"parts": [{"inlineData": {"data": base64.b64encode(expected).decode()}}]}}
            ]
        }
        return FakeResponse(payload=payload, headers={"Content-Type": "application/json"})

    _patch_post(monkeypatch, handler)

    output_path = tmp_path / "gemini.mp3"
    GeminiTTSAdapter().synthesize(
        "hello",
        {"api_key": "test-key", "base_url": "", "tts_model": "gemini-2.5-flash-preview-tts", "tts_voice": "Kore"},
        str(output_path),
    )

    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent"
    )
    assert captured["headers"]["x-goog-api-key"] == "test-key"
    assert captured["body"]["generationConfig"]["responseModalities"] == ["AUDIO"]
    assert captured["body"]["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"] == "Kore"
    assert output_path.read_bytes() == expected


def test_gemini_tts_wraps_raw_pcm_into_wav(tmp_path, monkeypatch):
    pcm = b"\x00\x01\x02\x03"

    def handler(url, headers, data):
        payload = {"candidates": [{"content": {"parts": [{"inlineData": {"data": base64.b64encode(pcm).decode()}}]}}]}
        return FakeResponse(payload=payload, headers={"Content-Type": "application/json"})

    _patch_post(monkeypatch, handler)

    output_path = tmp_path / "gemini.wav"
    GeminiTTSAdapter().synthesize("hello", {"api_key": "k", "base_url": ""}, str(output_path))

    audio = output_path.read_bytes()
    assert audio[:4] == b"RIFF"
    assert audio[-4:] == pcm


def test_glm_tts_uses_default_url_and_writes_binary(tmp_path, monkeypatch):
    expected = b"ID3 fake mp3"
    captured = {}

    def handler(url, headers, data):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(data)
        return FakeResponse(content=expected, headers={"Content-Type": "audio/mpeg"})

    _patch_post(monkeypatch, handler)

    output_path = tmp_path / "glm.mp3"
    GlmTTSAdapter().synthesize(
        "hello",
        {"api_key": "test-key", "base_url": "https://open.bigmodel.cn/api/paas/v4", "tts_voice": "tongtong"},
        str(output_path),
    )

    assert captured["url"] == "https://open.bigmodel.cn/api/paas/v4/audio/speech"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "glm-tts"
    assert output_path.read_bytes() == expected


def test_minimax_tts_decodes_json_audio_base64(tmp_path, monkeypatch):
    expected = b"minimax-audio-bytes"
    captured = {}

    def handler(url, headers, data):
        captured["url"] = url
        body = json.loads(data)
        assert body["voice_setting"]["voice_id"] == "male-qn-qingse"
        payload = {"data": {"audio": base64.b64encode(expected).decode()}}
        return FakeResponse(payload=payload, headers={"Content-Type": "application/json"})

    _patch_post(monkeypatch, handler)

    output_path = tmp_path / "minimax.mp3"
    MiniMaxTTSAdapter().synthesize(
        "hello",
        {"api_key": "test-key", "base_url": "https://api.minimaxi.com/v1", "tts_voice": "male-qn-qingse"},
        str(output_path),
    )

    assert captured["url"] == "https://api.minimaxi.com/v1/t2a_v2"
    assert output_path.read_bytes() == expected


def test_qwen_tts_normalizes_dashscope_url_and_decodes_audio(tmp_path, monkeypatch):
    expected = b"qwen-audio"
    captured = {}

    def handler(url, headers, data):
        captured["url"] = url
        body = json.loads(data)
        assert body["model"] == "qwen3-tts-flash"
        assert body["input"]["voice"] == "Cherry"
        assert body["input"]["language_type"] == "Chinese"
        payload = {"output": {"audio": {"data": base64.b64encode(expected).decode()}}}
        return FakeResponse(payload=payload, headers={"Content-Type": "application/json"})

    _patch_post(monkeypatch, handler)

    output_path = tmp_path / "qwen.mp3"
    QwenTTSAdapter().synthesize(
        "你好",
        {
            "api_key": "test-key",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "tts_model": "qwen3-tts-flash",
            "tts_voice": "Cherry",
            "language": "zh",
        },
        str(output_path),
    )

    assert captured["url"] == (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    )
    assert output_path.read_bytes() == expected


def test_minimax_tts_raises_when_no_audio(tmp_path, monkeypatch):
    def handler(url, headers, data):
        return FakeResponse(payload={"base_resp": {"status_code": 1004, "status_msg": "rate limit"}}, headers={})

    _patch_post(monkeypatch, handler)

    with pytest.raises(RuntimeError, match="未返回音频"):
        MiniMaxTTSAdapter().synthesize("hello", {"api_key": "k", "base_url": "https://api.minimaxi.com/v1"}, str(tmp_path / "x.mp3"))
