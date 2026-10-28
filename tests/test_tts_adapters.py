from pathlib import Path

import pytest

from nbot.services.tts_adapters.openai_adapter import OpenAITTSAdapter


class FakeResponse:
    def __init__(self, status_code=200, content=b"", headers=None, text=""):
        self.status_code = status_code
        self.content = content
        self.headers = headers or {}
        self.text = text

    def json(self):
        raise ValueError("not json")


def test_openai_tts_adapter_appends_v1_audio_speech_for_provider_base_url(tmp_path, monkeypatch):
    captured = {}

    def fake_post(url, headers, data, timeout):
        captured["url"] = url
        return FakeResponse(
            content=b"ID3 fake mp3",
            headers={"Content-Type": "audio/mpeg"},
        )

    monkeypatch.setattr("requests.post", fake_post)

    output_path = tmp_path / "speech.mp3"
    OpenAITTSAdapter().synthesize(
        "hello",
        {
            "api_key": "test-key",
            "base_url": "https://api.example.test",
            "tts_model": "gpt-4o-mini-tts",
            "tts_voice": "alloy",
            "tts_format": "mp3",
        },
        str(output_path),
    )

    assert captured["url"] == "https://api.example.test/v1/audio/speech"
    assert output_path.read_bytes() == b"ID3 fake mp3"


def test_openai_tts_adapter_rejects_html_response_instead_of_saving_mp3(tmp_path, monkeypatch):
    def fake_post(url, headers, data, timeout):
        return FakeResponse(
            content=b"<!DOCTYPE html><html></html>",
            headers={"Content-Type": "text/html"},
            text="landing page",
        )

    monkeypatch.setattr("requests.post", fake_post)

    output_path = tmp_path / "speech.mp3"
    with pytest.raises(RuntimeError, match="did not return audio"):
        OpenAITTSAdapter().synthesize(
            "hello",
            {
                "api_key": "test-key",
                "base_url": "https://api.example.test/v1",
                "tts_model": "gpt-4o-mini-tts",
                "tts_voice": "alloy",
                "tts_format": "mp3",
            },
            str(output_path),
        )

    assert not Path(output_path).exists()
