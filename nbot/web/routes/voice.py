"""
Voice APIs for TTS (Text-to-Speech) and STT (Speech-to-Text).
"""
import logging
import os
import threading
import time

from flask import jsonify, request, send_file
from werkzeug.utils import safe_join

_log = logging.getLogger(__name__)
_STT_MODEL = None
_STT_MODEL_NAME = None
_STT_MODEL_LOAD_ERROR = None
_STT_MODEL_LOCK = threading.Lock()
_MODEL_ROOT = os.path.abspath(os.path.join("data", "models", "faster-whisper"))


def _configure_model_root(base_data_dir: str) -> None:
    """Bind the faster-whisper cache directory to the web server data dir."""
    global _MODEL_ROOT

    base_dir = os.path.abspath(base_data_dir or os.path.join("data", "web"))
    shared_data_dir = os.path.abspath(os.path.join(base_dir, os.pardir))
    _MODEL_ROOT = os.path.join(shared_data_dir, "models", "faster-whisper")
    os.makedirs(_MODEL_ROOT, exist_ok=True)


def _get_local_model_dir(model_name: str) -> str:
    """Return the project-local model directory for a whisper model."""
    safe_name = str(model_name).replace("/", "--").replace("\\", "--").strip()
    return os.path.join(_MODEL_ROOT, safe_name)


def _resolve_cached_audio_path(cache_dir: str, filename: str):
    """Return a cache file path only when it stays inside the cache directory."""
    if not filename or filename != os.path.basename(filename):
        return None
    return safe_join(cache_dir, filename)


def _get_local_stt_config():
    """Return local faster-whisper settings with sensible defaults."""
    try:
        from nbot.web.utils.config_loader import get_stt_model_config

        stt_config = get_stt_model_config() or {}
    except Exception:
        stt_config = {}

    model_name = (
        stt_config.get("model")
        or os.environ.get("NBOT_FASTER_WHISPER_MODEL")
        or "tiny"
    )
    model_aliases = {
        "whisper-1": "tiny",
        "gpt-4o-mini-transcribe": "base",
        "gpt-4o-transcribe": "base",
    }
    model_name = model_aliases.get(str(model_name).strip(), model_name)
    language = stt_config.get("language") or os.environ.get("NBOT_STT_LANGUAGE") or "zh"
    device = stt_config.get("device") or os.environ.get("NBOT_FASTER_WHISPER_DEVICE") or "cpu"
    compute_type = (
        stt_config.get("compute_type")
        or os.environ.get("NBOT_FASTER_WHISPER_COMPUTE_TYPE")
        or "int8"
    )
    beam_size_raw = stt_config.get("beam_size") or os.environ.get("NBOT_FASTER_WHISPER_BEAM_SIZE") or 5
    try:
        beam_size = max(1, int(beam_size_raw))
    except (TypeError, ValueError):
        beam_size = 5

    return {
        "model_name": model_name,
        "model_path": _get_local_model_dir(model_name),
        "language": language,
        "device": device,
        "compute_type": compute_type,
        "beam_size": beam_size,
    }


def _ensure_stt_model_loaded(force_reload: bool = False):
    """Load the faster-whisper model once and reuse it for later requests."""
    global _STT_MODEL, _STT_MODEL_NAME, _STT_MODEL_LOAD_ERROR

    config = _get_local_stt_config()
    requested_model_name = config["model_name"]

    if (
        not force_reload
        and _STT_MODEL is not None
        and _STT_MODEL_NAME == requested_model_name
    ):
        return _STT_MODEL, config

    with _STT_MODEL_LOCK:
        if (
            not force_reload
            and _STT_MODEL is not None
            and _STT_MODEL_NAME == requested_model_name
        ):
            return _STT_MODEL, config

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            _STT_MODEL = None
            _STT_MODEL_NAME = None
            _STT_MODEL_LOAD_ERROR = (
                "faster-whisper is not installed. Run `pip install faster-whisper`."
            )
            raise RuntimeError(_STT_MODEL_LOAD_ERROR) from exc

        try:
            _log.info(
                "Loading faster-whisper model '%s' from %s on %s (%s)",
                requested_model_name,
                config["model_path"] if os.path.isdir(config["model_path"]) else "remote source",
                config["device"],
                config["compute_type"],
            )
            model_source = (
                config["model_path"]
                if os.path.isdir(config["model_path"])
                else requested_model_name
            )
            _STT_MODEL = WhisperModel(
                model_source,
                device=config["device"],
                compute_type=config["compute_type"],
            )
            _STT_MODEL_NAME = requested_model_name
            _STT_MODEL_LOAD_ERROR = None
            return _STT_MODEL, config
        except Exception as exc:
            _STT_MODEL = None
            _STT_MODEL_NAME = None
            _STT_MODEL_LOAD_ERROR = str(exc)
            raise RuntimeError(f"Failed to load faster-whisper model: {exc}") from exc


def _preload_stt_model():
    """Load the local STT model when the web service starts."""
    try:
        _ensure_stt_model_loaded()
    except Exception as exc:
        _log.warning("Failed to preload faster-whisper model: %s", exc)


def register_voice_routes(app, server):
    """Register voice-related API routes."""
    _configure_model_root(server.data_dir)
    _preload_stt_model()

    @app.route("/api/tts/synthesize", methods=["POST"])
    def tts_synthesize():
        """Convert text to speech."""
        try:
            data = request.json
            text = data.get("text", "")
            voice = data.get("voice", "zh-CN-XiaoxiaoNeural")
            speed = data.get("speed", 1.0)

            if not text:
                return jsonify({"error": "Text is required"}), 400

            from nbot.services.tts import _get_tts_config

            tts_config = _get_tts_config()
            api_key = tts_config.get("api_key")
            base_url = tts_config.get("base_url", "https://api.siliconflow.cn/v1")
            model = tts_config.get("model", "fnlp/MOSS-TTSD-v0.5")
            config_voice = tts_config.get("voice", "default")

            if not api_key:
                return jsonify({"error": "TTS API Key not configured"}), 400

            voice_to_use = config_voice if config_voice != "default" else voice

            temp_dir = os.path.join(server.data_dir, "tts_cache")
            os.makedirs(temp_dir, exist_ok=True)
            output_file = os.path.join(temp_dir, f"tts_{int(time.time())}.mp3")

            from openai import OpenAI

            client = OpenAI(api_key=api_key, base_url=base_url)

            with client.audio.speech.with_streaming_response.create(
                model=model,
                voice=voice_to_use,
                input=text,
                response_format="mp3",
            ) as response:
                response.stream_to_file(output_file)

            audio_url = f"/api/tts/audio/{os.path.basename(output_file)}"
            return jsonify({
                "success": True,
                "audio_url": audio_url,
                "text": text,
                "speed": speed,
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tts/audio/<filename>")
    def tts_audio(filename):
        """Serve generated TTS audio files."""
        try:
            temp_dir = os.path.join(server.data_dir, "tts_cache")
            file_path = _resolve_cached_audio_path(temp_dir, filename)

            if not file_path or not os.path.exists(file_path):
                return jsonify({"error": "Audio file not found"}), 404

            return send_file(file_path, mimetype="audio/mpeg")

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/stt/transcribe", methods=["POST"])
    def stt_transcribe():
        """Transcribe recorded audio with a local faster-whisper model."""
        try:
            if "audio" not in request.files:
                return jsonify({"error": "No audio file provided"}), 400

            audio_file = request.files["audio"]
            requested_language = request.form.get("language", "zh")

            temp_dir = os.path.join(server.data_dir, "stt_cache")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"stt_{int(time.time() * 1000)}.webm")
            audio_file.save(temp_path)

            try:
                model, config = _ensure_stt_model_loaded()
                language = requested_language or config["language"]
                beam_size = config["beam_size"]

                segments, info = model.transcribe(
                    temp_path,
                    beam_size=beam_size,
                    language=language,
                )
                text = "".join(segment.text for segment in segments).strip()

                return jsonify({
                    "success": True,
                    "text": text,
                    "language": getattr(info, "language", language) or language,
                    "model": config["model_name"],
                })
            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        except Exception as e:
            error_message = str(e)
            if _STT_MODEL_LOAD_ERROR and "Failed to load faster-whisper model" in error_message:
                error_message = _STT_MODEL_LOAD_ERROR
            return jsonify({"error": error_message}), 500
