import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from flask import Flask


def _load_voice_module():
    voice_path = Path(__file__).resolve().parents[1] / "nbot" / "web" / "routes" / "voice.py"
    spec = importlib.util.spec_from_file_location("voice_test_module", voice_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _load_tts_playground_module():
    route_path = Path(__file__).resolve().parents[1] / "nbot" / "web" / "routes" / "tts_playground.py"
    spec = importlib.util.spec_from_file_location("tts_playground_test_module", route_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


voice = _load_voice_module()
tts_playground = _load_tts_playground_module()
_TMP_ROOT = Path(__file__).resolve().parents[1] / "data" / "test_tmp"


class RepoTempDir:
    def __enter__(self):
        _TMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.path = tempfile.mkdtemp(dir=_TMP_ROOT)
        return self.path

    def __exit__(self, exc_type, exc, tb):
        shutil.rmtree(self.path, ignore_errors=True)


class VoiceRouteHelpersTest(unittest.TestCase):
    def test_configure_model_root_uses_project_data_dir(self):
        with RepoTempDir() as tmpdir:
            voice._configure_model_root(tmpdir)

            expected_root = os.path.join(os.path.dirname(tmpdir), "models", "faster-whisper")
            self.assertEqual(os.path.abspath(voice._MODEL_ROOT), os.path.abspath(expected_root))
            self.assertTrue(os.path.isdir(expected_root))
            self.assertEqual(
                os.path.abspath(voice._get_local_model_dir("tiny")),
                os.path.abspath(os.path.join(expected_root, "tiny")),
            )

    def test_resolve_cached_audio_path_rejects_path_traversal(self):
        with RepoTempDir() as tmpdir:
            ok_path = voice._resolve_cached_audio_path(tmpdir, "demo.mp3")
            bad_path = voice._resolve_cached_audio_path(tmpdir, "../secret.txt")
            nested_path = voice._resolve_cached_audio_path(tmpdir, "subdir/demo.mp3")

            self.assertEqual(
                os.path.abspath(ok_path),
                os.path.abspath(os.path.join(tmpdir, "demo.mp3")),
            )
            self.assertIsNone(bad_path)
            self.assertIsNone(nested_path)


class FakeTTSAdapter:
    def synthesize(self, text: str, config: dict, output_path: str) -> str:
        with open(output_path, "wb") as f:
            f.write(b"ID3 fake mp3 bytes")
        return output_path


class FakeWavTTSAdapter:
    def synthesize(self, text: str, config: dict, output_path: str) -> str:
        with open(output_path, "wb") as f:
            f.write(b"RIFF\x24\x00\x00\x00WAVEfmt fake wav bytes")
        return output_path


def _fake_tts_config():
    return {
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "provider_type": "openai_compatible",
        "tts_provider": "openai",
        "tts_model": "gpt-4o-mini-tts",
        "tts_voice": "default",
        "tts_speed": 1.0,
        "tts_pitch": 1.0,
        "tts_volume": 1.0,
        "tts_format": "mp3",
        "tts_url": "",
        "tts_headers": "",
        "tts_body_template": "",
    }


def _fake_tts_config_for_base_url(base_url: str):
    config = _fake_tts_config()
    config["base_url"] = base_url
    return config


def test_tts_playground_preview_generates_audio_url_without_importing_qq_tts(tmp_path, monkeypatch):
    app = Flask(__name__)
    server = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(tts_playground, "get_tts_config_from_server", lambda *_: _fake_tts_config())
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda provider: FakeTTSAdapter())

    tts_playground.register_tts_playground_routes(app, server)

    response = app.test_client().post(
        "/api/tts/preview",
        json={"text": "hello", "voice": "alloy", "speed": 1.25, "pitch": 1.0, "volume": 1.0},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["audio_url"].startswith("/api/tts/audio/tts_preview_")
    assert (tmp_path / "tts_cache").exists()


def test_tts_playground_preview_uses_requested_tts_model_id(tmp_path, monkeypatch):
    captured = {}

    class CapturingAdapter(FakeTTSAdapter):
        def synthesize(self, text: str, config: dict, output_path: str) -> str:
            captured["base_url"] = config.get("base_url")
            return super().synthesize(text, config, output_path)

    app = Flask(__name__)
    server = SimpleNamespace(
        data_dir=str(tmp_path),
        active_models_by_purpose={"tts": "active-tts"},
        ai_models=[
            {
                "id": "first-tts",
                "purpose": "tts",
                "enabled": True,
                "api_key": "test-key",
                "base_url": "https://first.example.test/v1",
                "model": "tts-first",
                "provider_type": "openai_compatible",
                "tts_provider": "openai",
                "tts_voice": "alloy",
            },
            {
                "id": "active-tts",
                "purpose": "tts",
                "enabled": True,
                "api_key": "test-key",
                "base_url": "https://active.example.test/v1",
                "model": "tts-active",
                "provider_type": "openai_compatible",
                "tts_provider": "openai",
                "tts_voice": "nova",
            },
        ],
    )
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda provider: CapturingAdapter())

    tts_playground.register_tts_playground_routes(app, server)

    response = app.test_client().post(
        "/api/tts/preview",
        json={
            "model_id": "active-tts",
            "text": "hello",
            "voice": "alloy",
            "speed": 1.0,
            "pitch": 1.0,
            "volume": 1.0,
        },
    )

    assert response.status_code == 200
    assert captured["base_url"] == "https://active.example.test/v1"


def test_tts_playground_preview_uses_requested_voice_not_model_default(tmp_path, monkeypatch):
    captured = {}

    class CapturingAdapter(FakeTTSAdapter):
        def synthesize(self, text: str, config: dict, output_path: str) -> str:
            captured["voice"] = config.get("tts_voice")
            return super().synthesize(text, config, output_path)

    app = Flask(__name__)
    server = SimpleNamespace(
        data_dir=str(tmp_path),
        active_models_by_purpose={"tts": "active-tts"},
        ai_models=[
            {
                "id": "active-tts",
                "purpose": "tts",
                "enabled": True,
                "api_key": "test-key",
                "base_url": "https://active.example.test/v1",
                "model": "tts-active",
                "provider_type": "openai_compatible",
                "tts_provider": "openai",
                "tts_voice": "nova",
            },
        ],
    )
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda provider: CapturingAdapter())

    tts_playground.register_tts_playground_routes(app, server)

    response = app.test_client().post(
        "/api/tts/preview",
        json={
            "model_id": "active-tts",
            "text": "hello",
            "voice": "echo",
            "speed": 1.0,
            "pitch": 1.0,
            "volume": 1.0,
        },
    )

    assert response.status_code == 200
    assert captured["voice"] == "echo"


def test_tts_synthesize_returns_selected_speed_and_audio_url(tmp_path, monkeypatch):
    app = Flask(__name__)
    server = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(voice, "_preload_stt_model", lambda: None)
    monkeypatch.setattr(voice, "get_tts_config_from_server", lambda *_: _fake_tts_config())
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda provider: FakeTTSAdapter())

    voice.register_voice_routes(app, server)

    response = app.test_client().post(
        "/api/tts/synthesize",
        json={"text": "hello", "voice": "nova", "speed": 1.5},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["speed"] == 1.5
    assert payload["audio_url"].startswith("/api/tts/audio/tts_")


def test_tts_synthesize_renames_file_when_actual_audio_format_differs(tmp_path, monkeypatch):
    app = Flask(__name__)
    server = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(voice, "_preload_stt_model", lambda: None)
    monkeypatch.setattr(voice, "get_tts_config_from_server", lambda *_: _fake_tts_config())
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda provider: FakeWavTTSAdapter())

    voice.register_voice_routes(app, server)

    response = app.test_client().post(
        "/api/tts/synthesize",
        json={"text": "hello", "voice": "nova", "speed": 1.5},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["audio_url"].endswith(".wav")

    audio_response = app.test_client().get(payload["audio_url"])
    assert audio_response.status_code == 200
    assert audio_response.mimetype == "audio/wav"
    assert audio_response.headers["Accept-Ranges"] == "bytes"

    range_response = app.test_client().get(
        payload["audio_url"],
        headers={"Range": "bytes=0-11"},
    )
    assert range_response.status_code == 206
    assert range_response.headers["Accept-Ranges"] == "bytes"
    assert range_response.headers["Content-Range"].startswith("bytes 0-11/")
    assert range_response.data == b"RIFF\x24\x00\x00\x00WAVE"


def test_preview_uses_actual_audio_format_and_supports_browser_ranges(tmp_path, monkeypatch):
    app = Flask(__name__)
    server = SimpleNamespace(data_dir=str(tmp_path))
    monkeypatch.setattr(voice, "_preload_stt_model", lambda: None)
    monkeypatch.setattr(voice, "get_tts_config_from_server", lambda *_: _fake_tts_config())
    monkeypatch.setattr(tts_playground, "get_tts_config_from_server", lambda *_: _fake_tts_config())
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda _: FakeWavTTSAdapter())
    voice.register_voice_routes(app, server)
    tts_playground.register_tts_playground_routes(app, server)
    client = app.test_client()
    first = client.post("/api/tts/preview", json={"text": "first"}).get_json()["audio_url"]
    second = client.post("/api/tts/preview", json={"text": "second"}).get_json()["audio_url"]
    assert first != second
    assert first.endswith(".wav") and second.endswith(".wav")
    response = client.get(first, headers={"Range": "bytes=0-11"})
    assert response.status_code == 206
    assert response.mimetype == "audio/wav"
    assert response.data == b"RIFF\x24\x00\x00\x00WAVE"
    assert client.get(first, headers={"Range": "bytes=9999-"}).status_code == 416


def test_tts_synthesize_uses_active_tts_model_from_server(tmp_path, monkeypatch):
    captured = {}

    class CapturingAdapter(FakeTTSAdapter):
        def synthesize(self, text: str, config: dict, output_path: str) -> str:
            captured["base_url"] = config.get("base_url")
            return super().synthesize(text, config, output_path)

    app = Flask(__name__)
    server = SimpleNamespace(
        data_dir=str(tmp_path),
        active_models_by_purpose={"tts": "active-tts"},
        ai_models=[
            {
                "id": "first-tts",
                "purpose": "tts",
                "enabled": True,
                "api_key": "test-key",
                "base_url": "https://first.example.test/v1",
                "model": "tts-first",
                "provider_type": "openai_compatible",
                "tts_provider": "openai",
                "tts_voice": "alloy",
            },
            {
                "id": "active-tts",
                "purpose": "tts",
                "enabled": True,
                "api_key": "test-key",
                "base_url": "https://active.example.test/v1",
                "model": "tts-active",
                "provider_type": "openai_compatible",
                "tts_provider": "openai",
                "tts_voice": "nova",
            },
        ],
    )
    monkeypatch.setattr(voice, "_preload_stt_model", lambda: None)
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda provider: CapturingAdapter())

    voice.register_voice_routes(app, server)

    response = app.test_client().post(
        "/api/tts/synthesize",
        json={"text": "hello", "voice": "alloy", "speed": 1.0},
    )

    assert response.status_code == 200
    assert captured["base_url"] == "https://active.example.test/v1"


def test_tts_synthesize_uses_requested_voice_not_model_default(tmp_path, monkeypatch):
    captured = {}

    class CapturingAdapter(FakeTTSAdapter):
        def synthesize(self, text: str, config: dict, output_path: str) -> str:
            captured["voice"] = config.get("tts_voice")
            return super().synthesize(text, config, output_path)

    app = Flask(__name__)
    server = SimpleNamespace(
        data_dir=str(tmp_path),
        active_models_by_purpose={"tts": "active-tts"},
        ai_models=[
            {
                "id": "active-tts",
                "purpose": "tts",
                "enabled": True,
                "api_key": "test-key",
                "base_url": "https://active.example.test/v1",
                "model": "tts-active",
                "provider_type": "openai_compatible",
                "tts_provider": "openai",
                "tts_voice": "nova",
            },
        ],
    )
    monkeypatch.setattr(voice, "_preload_stt_model", lambda: None)
    monkeypatch.setattr("nbot.services.tts_adapters.get_adapter", lambda provider: CapturingAdapter())

    voice.register_voice_routes(app, server)

    response = app.test_client().post(
        "/api/tts/synthesize",
        json={"text": "hello", "voice": "echo", "speed": 1.0},
    )

    assert response.status_code == 200
    assert captured["voice"] == "echo"


if __name__ == "__main__":
    unittest.main()
