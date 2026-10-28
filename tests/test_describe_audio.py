import base64
import os

from nbot.services.ai import AIClient, _data_url_to_tempfile


def _make_client():
    return AIClient(
        api_key="test",
        base_url="https://api.example.com/v1",
        model="demo-model",
        pic_model="",
        search_api_key="",
        search_api_url="",
        video_api="",
    )


def test_describe_audio_from_local_path(monkeypatch, tmp_path):
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"fake-audio")
    seen = {}

    def fake_transcribe(path, language=None):
        seen["path"] = path
        return "你好呀"

    monkeypatch.setattr("nbot.services.stt.transcribe", fake_transcribe)

    assert _make_client().describe_audio(str(audio)) == "你好呀"
    assert seen["path"] == str(audio)


def test_describe_audio_from_data_url(monkeypatch):
    captured = {}

    def fake_transcribe(path, language=None):
        captured["path"] = path
        captured["data"] = open(path, "rb").read()
        return "  转写结果  "

    monkeypatch.setattr("nbot.services.stt.transcribe", fake_transcribe)

    payload = base64.b64encode(b"voice-bytes").decode("utf-8")
    result = _make_client().describe_audio(f"data:audio/mpeg;base64,{payload}")

    assert result == "转写结果"
    assert captured["data"] == b"voice-bytes"
    assert not os.path.exists(captured["path"])


def test_describe_audio_from_http_url(monkeypatch):
    seen = {}

    def fake_transcribe_from_url(url, language=None):
        seen["url"] = url
        return "remote text"

    monkeypatch.setattr("nbot.services.stt.transcribe_from_url", fake_transcribe_from_url)

    assert _make_client().describe_audio("https://example.com/a.mp3") == "remote text"
    assert seen["url"] == "https://example.com/a.mp3"


def test_describe_audio_handles_missing_and_failures(monkeypatch):
    client = _make_client()
    assert client.describe_audio("") is None
    assert client.describe_audio("/no/such/file.mp3") is None

    monkeypatch.setattr("nbot.services.stt.transcribe", lambda path, language=None: None)
    assert client.describe_audio("data:audio/mpeg;base64," + base64.b64encode(b"x").decode()) is None


def test_data_url_to_tempfile_rejects_garbage():
    assert _data_url_to_tempfile("not-a-data-url") is None
