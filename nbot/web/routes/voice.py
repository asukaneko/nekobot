"""
Voice APIs for TTS (Text-to-Speech) and STT (Speech-to-Text).
"""
import logging
import os
import threading
import time

from flask import jsonify, request, send_file
from werkzeug.exceptions import HTTPException
from werkzeug.utils import safe_join

from nbot.services.tts_config import get_tts_config_from_server
from nbot.web.audio_cache import normalize_generated_audio_path

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
        "enabled": bool(stt_config.get("local_enabled", False)),
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
    if not config["enabled"]:
        _STT_MODEL = None
        _STT_MODEL_NAME = None
        _STT_MODEL_LOAD_ERROR = None
        raise RuntimeError("Local STT is disabled. Set [stt] local_enabled = true in config.ini to enable faster-whisper.")

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
    if not _get_local_stt_config()["enabled"]:
        _log.info("Local STT preload skipped because [stt] local_enabled is false")
        return

    try:
        _ensure_stt_model_loaded()
    except Exception as exc:
        _log.warning("Failed to preload faster-whisper model: %s", exc)


def _get_stt_config_for_adapter(server) -> dict:
    """Resolve STT config from web server state for adapter calls."""
    from nbot.services.tts_config import normalize_stt_config

    models = getattr(server, "ai_models", []) or []
    active_model_id = (
        (getattr(server, "active_models_by_purpose", {}) or {}).get("stt")
    )

    if active_model_id:
        for model in models:
            if model.get("id") == active_model_id and model.get("enabled", True):
                return normalize_stt_config(model)

    for model in models:
        if model.get("purpose", "chat") == "stt" and model.get("enabled", True):
            return normalize_stt_config(model)

    # 没有云端 STT 配置，回退到本地配置
    local_cfg = _get_local_stt_config()
    return {
        "api_key": "",
        "base_url": "",
        "provider_type": "local" if local_cfg["enabled"] else "",
        "stt_provider": "local" if local_cfg["enabled"] else "",
        "stt_model": local_cfg["model_name"],
        "stt_language": local_cfg["language"],
        "device": local_cfg["device"],
        "compute_type": local_cfg["compute_type"],
        "beam_size": local_cfg["beam_size"],
    }


def _register_realtime_routes(app, server):
    """P2-13 Realtime 实时语音：provider 列表 / 会话状态 + Socket.IO 命名空间。"""
    try:
        from nbot.web import realtime_voice
    except Exception as exc:  # noqa: BLE001 - 缺依赖时不拖垮语音路由
        _log.warning("[Realtime] 导入实时语音模块失败: %s", exc)
        return

    try:
        registered = realtime_voice.register_realtime_voice(server)
    except Exception as exc:  # noqa: BLE001
        _log.warning("[Realtime] 注册实时语音命名空间失败: %s", exc)
        registered = False

    @app.route("/api/realtime/providers", methods=["GET"])
    def realtime_providers_route():
        """可用的 Realtime provider 及配置状态（前端下拉用）。"""
        try:
            providers = realtime_voice.list_realtime_providers()
            default_id = next(
                (item["id"] for item in providers if item.get("default")), "qwen_omni"
            )
            return jsonify({
                "success": True,
                "providers": providers,
                "default_provider": default_id,
                "namespace": realtime_voice.REALTIME_NAMESPACE,
                "registered": registered,
                "audio": {
                    "format": "pcm16",
                    "sample_rate": 24000,
                    "channels": 1,
                    "transport": "socket.io base64 chunks",
                },
            })
        except Exception as exc:  # noqa: BLE001
            _log.error("[Realtime] providers 失败: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/realtime/status", methods=["GET"])
    def realtime_status_route():
        """当前实时语音会话数与统计。"""
        try:
            manager = getattr(server, "_realtime_voice_manager", None)
            stats = manager.stats() if manager is not None else {"active_sessions": 0, "sessions": []}
            return jsonify({
                "success": True,
                "registered": registered,
                "namespace": realtime_voice.REALTIME_NAMESPACE,
                **stats,
            })
        except Exception as exc:  # noqa: BLE001
            _log.error("[Realtime] status 失败: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500


def register_voice_routes(app, server):
    """Register voice-related API routes."""
    _configure_model_root(server.data_dir)
    _preload_stt_model()
    _register_realtime_routes(app, server)

    _VALID_VOICES = frozenset({
        "alloy", "echo", "fable", "onyx", "nova", "shimmer",
        "coral", "verse", "ballad", "ash", "sage", "marin", "cedar",
    })

    @app.route("/api/tts/synthesize", methods=["POST"])
    def tts_synthesize():
        """Convert text to speech."""
        try:
            data = request.json
            text = data.get("text", "")
            request_model_id = data.get("model_id", "")
            request_voice = data.get("voice", "alloy")
            request_speed = data.get("speed", 1.0)

            if not text:
                return jsonify({"error": "Text is required"}), 400

            from nbot.services.tts_adapters import get_adapter
            from nbot.services.tts_adapters.xiaomi_adapter import XIAOMI_VOICES

            tts_config = get_tts_config_from_server(server, request_model_id)
            if not tts_config.get("api_key"):
                return jsonify({"error": "TTS API Key not configured"}), 400

            provider = tts_config.get("tts_provider") or tts_config.get("provider_type", "")

            voice_to_use = (request_voice or tts_config.get("tts_voice") or "alloy").strip()

            if provider == "xiaomi":
                xiaomi_voice_ids = frozenset(v["id"] for v in XIAOMI_VOICES)
                if voice_to_use not in xiaomi_voice_ids:
                    voice_to_use = "mimo_default"
            else:
                voice_to_use = voice_to_use.strip().lower() if voice_to_use else "alloy"
                if voice_to_use not in _VALID_VOICES:
                    _log.warning("Invalid TTS voice '%s', falling back to 'alloy'", voice_to_use)
                    voice_to_use = "alloy"

            tts_config["tts_voice"] = voice_to_use
            speed_to_use = max(0.25, min(4.0, float(request_speed)))
            tts_config["tts_speed"] = speed_to_use

            temp_dir = os.path.join(server.data_dir, "tts_cache")
            os.makedirs(temp_dir, exist_ok=True)
            requested_ext = "." + str(tts_config.get("tts_format") or "mp3").lower()
            if requested_ext not in (".mp3", ".wav", ".opus", ".ogg", ".flac", ".m4a", ".aac", ".amr"):
                requested_ext = ".mp3"
            output_file = os.path.join(temp_dir, f"tts_{time.time_ns()}{requested_ext}")

            adapter = get_adapter(provider)
            adapter.synthesize(text, tts_config, output_file)
            if not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
                raise RuntimeError("TTS adapter did not produce an audio file")
            output_file = normalize_generated_audio_path(output_file)

            audio_url = f"/api/tts/audio/{os.path.basename(output_file)}"
            return jsonify({
                "success": True,
                "audio_url": audio_url,
                "text": text,
                "speed": speed_to_use,
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tts/audio/<filename>")
    def tts_audio(filename):
        """Serve generated TTS audio files."""
        try:
            temp_dir = os.path.abspath(os.path.join(server.data_dir, "tts_cache"))
            file_path = _resolve_cached_audio_path(temp_dir, filename)

            if not file_path or not os.path.exists(file_path):
                _log.warning(f"TTS audio not found: dir={temp_dir}, file={filename}, resolved={file_path}")
                return jsonify({"error": "Audio file not found"}), 404

            # 根据扩展名选择 mimetype
            ext = os.path.splitext(filename)[1].lower()
            mime_map = {
                ".mp3": "audio/mpeg",
                ".wav": "audio/wav",
                ".opus": "audio/opus",
                ".flac": "audio/flac",
                ".ogg": "audio/ogg",
                ".m4a": "audio/mp4",
                ".aac": "audio/aac",
                ".amr": "audio/amr",
            }
            mimetype = mime_map.get(ext, "audio/mpeg")
            response = send_file(file_path, mimetype=mimetype, conditional=True)
            response.headers["Accept-Ranges"] = "bytes"
            return response

        except HTTPException:
            raise
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/stt/transcribe", methods=["POST"])
    def stt_transcribe():
        """Transcribe recorded audio using the configured STT adapter."""
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
                stt_config = _get_stt_config_for_adapter(server)
                provider = stt_config.get("stt_provider") or stt_config.get("provider_type", "")

                # 如果没有配置云端 STT，回退到本地 faster-whisper
                if not provider or provider in ("local", "faster_whisper"):
                    if not _get_local_stt_config()["enabled"]:
                        return jsonify({
                            "error": "STT 未配置。请在 AI 模型中添加 stt 类型的模型，"
                                     "或在 config.ini 中设置 [stt] local_enabled = true 启用本地识别。"
                        }), 400

                from nbot.services.stt_adapters import get_adapter
                adapter = get_adapter(provider)
                text = adapter.transcribe(temp_path, stt_config, requested_language)

                return jsonify({
                    "success": True,
                    "text": text,
                    "language": requested_language,
                    "provider": provider or "local",
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
