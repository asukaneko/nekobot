"""
TTS Playground routes for voice selection, preview, and custom voice upload.
"""
import logging
import os
import time

from flask import jsonify, request
from werkzeug.utils import secure_filename

from nbot.services.tts_config import get_tts_config_from_server
from nbot.web.audio_cache import normalize_generated_audio_path

_log = logging.getLogger(__name__)

# Built-in voice catalog with descriptions
VOICE_CATALOG = [
    {"id": "alloy", "name": "Alloy", "description": "中性均衡 - Neutral, balanced tone"},
    {"id": "echo", "name": "Echo", "description": "温暖共鸣 - Warm, resonant"},
    {"id": "fable", "name": "Fable", "description": "富有表现力 - Expressive, storytelling"},
    {"id": "onyx", "name": "Onyx", "description": "深沉权威 - Deep, authoritative"},
    {"id": "nova", "name": "Nova", "description": "明亮活力 - Bright, energetic"},
    {"id": "shimmer", "name": "Shimmer", "description": "柔和温柔 - Soft, gentle"},
    {"id": "coral", "name": "Coral", "description": "友好对话 - Friendly, conversational"},
    {"id": "verse", "name": "Verse", "description": "诗意韵律 - Poetic, melodic"},
    {"id": "ballad", "name": "Ballad", "description": "流畅抒情 - Smooth, lyrical"},
    {"id": "ash", "name": "Ash", "description": "沉稳从容 - Calm, measured"},
    {"id": "sage", "name": "Sage", "description": "睿智深思 - Wise, thoughtful"},
    {"id": "marin", "name": "Marin", "description": "清晰明快 - Clear, crisp"},
    {"id": "cedar", "name": "Cedar", "description": "温暖自然 - Warm, natural"},
]

_VALID_VOICES = frozenset(v["id"] for v in VOICE_CATALOG)

_CUSTOM_VOICES_FILE = "custom_voices.json"
_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB


def _get_custom_voices(data_dir: str) -> list:
    """Load custom voices from persistent storage."""
    import json
    file_path = os.path.join(data_dir, _CUSTOM_VOICES_FILE)
    if not os.path.exists(file_path):
        return []
    try:
        with open(file_path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_custom_voices(data_dir: str, voices: list) -> None:
    """Save custom voices to persistent storage."""
    import json
    file_path = os.path.join(data_dir, _CUSTOM_VOICES_FILE)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(voices, f, ensure_ascii=False, indent=2)


def register_tts_playground_routes(app, server):
    """Register TTS playground API routes."""

    @app.route("/api/tts/voices", methods=["GET"])
    def tts_voices():
        """Return the list of available TTS voices based on provider."""
        try:
            from nbot.services.tts_adapters.doubao_adapter import DOUBAO_VOICES
            from nbot.services.tts_adapters.gemini_adapter import GEMINI_VOICES
            from nbot.services.tts_adapters.glm_adapter import GLM_VOICES
            from nbot.services.tts_adapters.minimax_adapter import MINIMAX_VOICES
            from nbot.services.tts_adapters.qwen_adapter import QWEN_VOICES
            from nbot.services.tts_adapters.xiaomi_adapter import XIAOMI_VOICES

            request_model_id = request.args.get("model_id", "")
            tts_config = get_tts_config_from_server(server, request_model_id)
            provider = tts_config.get("tts_provider") or tts_config.get("provider_type", "")

            if provider == "xiaomi":
                voices = list(XIAOMI_VOICES)
            elif provider in ("doubao", "volcengine"):
                voices = list(DOUBAO_VOICES)
            elif provider in ("gemini", "google", "google_gemini"):
                voices = list(GEMINI_VOICES)
            elif provider in ("glm", "zhipu", "bigmodel"):
                voices = list(GLM_VOICES)
            elif provider == "minimax":
                voices = list(MINIMAX_VOICES)
            elif provider in ("qwen", "dashscope", "tongyi"):
                voices = list(QWEN_VOICES)
            else:
                voices = list(VOICE_CATALOG)
                custom_voices = _get_custom_voices(server.data_dir)
                voices.extend(custom_voices)
            return jsonify({"success": True, "voices": voices})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tts/preview", methods=["POST"])
    def tts_preview():
        """Generate TTS audio preview with given parameters."""
        try:
            data = request.json
            text = data.get("text", "")
            request_model_id = data.get("model_id", "")
            request_voice = data.get("voice", "alloy")
            request_speed = data.get("speed", 1.0)
            request_pitch = data.get("pitch", 1.0)
            request_volume = data.get("volume", 1.0)

            if not text:
                return jsonify({"error": "Text is required"}), 400

            from nbot.services.tts_adapters import get_adapter
            from nbot.services.tts_adapters.doubao_adapter import DOUBAO_VOICES
            from nbot.services.tts_adapters.gemini_adapter import GEMINI_VOICES
            from nbot.services.tts_adapters.glm_adapter import GLM_VOICES
            from nbot.services.tts_adapters.minimax_adapter import MINIMAX_VOICES
            from nbot.services.tts_adapters.qwen_adapter import QWEN_VOICES
            from nbot.services.tts_adapters.xiaomi_adapter import XIAOMI_VOICES

            tts_config = get_tts_config_from_server(server, request_model_id)
            if not tts_config.get("api_key"):
                return jsonify({"error": "TTS API Key not configured"}), 400

            provider = tts_config.get("tts_provider") or tts_config.get("provider_type", "")

            voice_to_use = (request_voice or tts_config.get("tts_voice") or "alloy").strip()

            # 根据提供商校验音色
            if provider == "xiaomi":
                xiaomi_voice_ids = frozenset(v["id"] for v in XIAOMI_VOICES)
                if voice_to_use not in xiaomi_voice_ids:
                    voice_to_use = "mimo_default"
            elif provider in ("doubao", "volcengine"):
                doubao_voice_ids = frozenset(v["id"] for v in DOUBAO_VOICES)
                if voice_to_use not in doubao_voice_ids:
                    voice_to_use = "zh_female_shuangkuaisisi_moon_bigtts"
            elif provider in ("gemini", "google", "google_gemini"):
                gemini_voice_ids = frozenset(v["id"] for v in GEMINI_VOICES)
                if voice_to_use not in gemini_voice_ids:
                    voice_to_use = "Kore"
            elif provider in ("glm", "zhipu", "bigmodel"):
                glm_voice_ids = frozenset(v["id"] for v in GLM_VOICES)
                if voice_to_use not in glm_voice_ids:
                    voice_to_use = "tongtong"
            elif provider == "minimax":
                minimax_voice_ids = frozenset(v["id"] for v in MINIMAX_VOICES)
                if voice_to_use not in minimax_voice_ids:
                    voice_to_use = "male-qn-qingse"
            elif provider in ("qwen", "dashscope", "tongyi"):
                qwen_voice_ids = frozenset(v["id"] for v in QWEN_VOICES)
                if voice_to_use not in qwen_voice_ids:
                    voice_to_use = "Cherry"
            else:
                voice_to_use = voice_to_use.strip().lower() if voice_to_use else "alloy"
                custom_voices = _get_custom_voices(server.data_dir)
                custom_voice_ids = frozenset(v["id"] for v in custom_voices)
                if voice_to_use not in (_VALID_VOICES | custom_voice_ids):
                    voice_to_use = "alloy"

            # Clamp parameters
            speed = max(0.25, min(4.0, float(request_speed)))
            pitch = max(0.5, min(2.0, float(request_pitch)))
            volume = max(0.0, min(2.0, float(request_volume)))

            # Override config with frontend params for preview
            tts_config["tts_voice"] = voice_to_use
            tts_config["tts_speed"] = speed
            tts_config["tts_pitch"] = pitch
            tts_config["tts_volume"] = volume

            temp_dir = os.path.join(server.data_dir, "tts_cache")
            os.makedirs(temp_dir, exist_ok=True)
            output_file = os.path.join(temp_dir, f"tts_preview_{time.time_ns()}.mp3")

            adapter = get_adapter(provider)
            adapter.synthesize(text, tts_config, output_file)
            if not os.path.exists(output_file) or os.path.getsize(output_file) <= 0:
                raise RuntimeError("TTS adapter did not produce an audio file")
            output_file = normalize_generated_audio_path(output_file)

            audio_url = f"/api/tts/audio/{os.path.basename(output_file)}"
            return jsonify({
                "success": True,
                "audio_url": audio_url,
                "voice": voice_to_use,
                "speed": speed,
                "pitch": pitch,
                "volume": volume,
            })

        except Exception as e:
            _log.error("TTS preview failed: %s", e)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tts/upload-voice", methods=["POST"])
    def tts_upload_voice():
        """Upload a custom voice sample."""
        try:
            if "file" not in request.files:
                return jsonify({"error": "No audio file provided"}), 400

            audio_file = request.files["file"]
            custom_name = request.form.get("customName", "").strip()
            sample_text = request.form.get("text", "").strip()

            if not custom_name:
                return jsonify({"error": "Voice name is required"}), 400

            if not sample_text:
                return jsonify({"error": "Sample text is required"}), 400

            # Check file size
            audio_file.seek(0, 2)
            file_size = audio_file.tell()
            audio_file.seek(0)

            if file_size > _MAX_UPLOAD_SIZE:
                return jsonify({"error": "File size exceeds 10MB limit"}), 400

            # Get TTS config for API key
            tts_config = get_tts_config_from_server(server)
            api_key = tts_config.get("api_key", "")

            if not api_key:
                return jsonify({"error": "TTS API Key not configured"}), 400

            # Save temp file
            temp_dir = os.path.join(server.data_dir, "tts_upload_cache")
            os.makedirs(temp_dir, exist_ok=True)
            filename = secure_filename(audio_file.filename) or f"voice_{int(time.time())}.wav"
            temp_path = os.path.join(temp_dir, filename)
            audio_file.save(temp_path)

            try:
                import requests as http_requests

                base_url = tts_config.get("base_url") or "https://api.siliconflow.cn/v1"
                model = tts_config.get("model") or "gpt-4o-mini-tts"
                custom_upload_url = tts_config.get("tts_upload_url", "")

                # 优先使用自定义上传URL
                if custom_upload_url:
                    url = custom_upload_url
                else:
                    url = f"{base_url.rstrip('/')}/uploads/audio/voice"
                headers = {"Authorization": f"Bearer {api_key}"}

                with open(temp_path, "rb") as f:
                    files = {"file": (filename, f)}
                    data = {
                        "model": model,
                        "customName": custom_name,
                        "text": sample_text,
                    }
                    from nbot.core.model_proxy import model_proxy_request_kwargs

                    resp = http_requests.post(
                        url,
                        headers=headers,
                        files=files,
                        data=data,
                        timeout=60,
                        **model_proxy_request_kwargs(
                            tts_config.get("proxy_url", "")
                        ),
                    )

                if resp.status_code != 200:
                    return jsonify({
                        "error": f"Voice upload failed: HTTP {resp.status_code} - {resp.text}"
                    }), 500

                result = resp.json()
                voice_id = result.get("voice_id") or result.get("uri") or custom_name

                # Save to custom voices list
                custom_voices = _get_custom_voices(server.data_dir)
                custom_voices.append({
                    "id": voice_id,
                    "name": custom_name,
                    "description": f"自定义音色 - Custom voice: {custom_name}",
                    "sample_text": sample_text,
                    "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })
                _save_custom_voices(server.data_dir, custom_voices)

                return jsonify({
                    "success": True,
                    "voice_id": voice_id,
                    "name": custom_name,
                })

            finally:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

        except Exception as e:
            _log.error("Voice upload failed: %s", e)
            return jsonify({"error": str(e)}), 500
