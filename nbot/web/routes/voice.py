"""
语音相关API路由
TTS (Text-to-Speech) 和 STT (Speech-to-Text)
"""
import os
import tempfile
import time
from flask import request, jsonify


def register_voice_routes(app, server):
    """注册语音相关路由"""

    @app.route("/api/tts/synthesize", methods=["POST"])
    def tts_synthesize():
        """将文字转换为语音"""
        try:
            data = request.json
            text = data.get("text", "")
            voice = data.get("voice", "zh-CN-XiaoxiaoNeural")
            speed = data.get("speed", 1.0)

            if not text:
                return jsonify({"error": "Text is required"}), 400

            # 获取TTS配置
            from nbot.services.tts import _get_tts_config
            tts_config = _get_tts_config()
            api_key = tts_config.get("api_key")
            base_url = tts_config.get("base_url", "https://api.siliconflow.cn/v1")
            model = tts_config.get("model", "fnlp/MOSS-TTSD-v0.5")
            config_voice = tts_config.get("voice", "default")

            if not api_key:
                return jsonify({"error": "TTS API Key not configured"}), 400

            # 使用配置的voice或请求的voice
            voice_to_use = config_voice if config_voice != "default" else voice

            # 创建临时文件
            temp_dir = os.path.join(server.data_dir, "tts_cache")
            os.makedirs(temp_dir, exist_ok=True)
            output_file = os.path.join(temp_dir, f"tts_{int(time.time())}.mp3")

            # 调用TTS API
            from openai import OpenAI
            client = OpenAI(api_key=api_key, base_url=base_url)

            with client.audio.speech.with_streaming_response.create(
                model=model,
                voice=voice_to_use,
                input=text,
                response_format="mp3"
            ) as response:
                response.stream_to_file(output_file)

            # 返回音频URL
            audio_url = f"/api/tts/audio/{os.path.basename(output_file)}"
            return jsonify({
                "success": True,
                "audio_url": audio_url,
                "text": text
            })

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/tts/audio/<filename>")
    def tts_audio(filename):
        """获取TTS生成的音频文件"""
        try:
            temp_dir = os.path.join(server.data_dir, "tts_cache")
            file_path = os.path.join(temp_dir, filename)

            if not os.path.exists(file_path):
                return jsonify({"error": "Audio file not found"}), 404

            from flask import send_file
            return send_file(file_path, mimetype="audio/mpeg")

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/stt/transcribe", methods=["POST"])
    def stt_transcribe():
        """将语音转换为文字"""
        try:
            if "audio" not in request.files:
                return jsonify({"error": "No audio file provided"}), 400

            audio_file = request.files["audio"]
            language = request.form.get("language", "zh")

            # 保存临时文件
            temp_dir = os.path.join(server.data_dir, "stt_cache")
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"stt_{int(time.time())}.webm")
            audio_file.save(temp_path)

            try:
                # 获取STT配置
                from nbot.services.stt import _get_stt_config
                stt_config = _get_stt_config()
                api_key = stt_config.get("api_key")
                base_url = stt_config.get("base_url", "")
                model = stt_config.get("model", "whisper-1")
                config_language = stt_config.get("language", "zh")

                if not api_key:
                    return jsonify({"error": "STT API Key not configured"}), 400

                # 使用配置的语言或请求的语言
                lang = language if language else config_language

                # 构建请求URL
                if base_url:
                    url = base_url.rstrip("/") + "/audio/transcriptions"
                else:
                    url = "https://api.openai.com/v1/audio/transcriptions"

                import requests

                headers = {
                    "Authorization": f"Bearer {api_key}"
                }

                data = {
                    "model": model,
                    "language": lang,
                    "response_format": "text"
                }

                with open(temp_path, "rb") as f:
                    files = {
                        "file": (os.path.basename(temp_path), f, "audio/webm")
                    }
                    response = requests.post(url, headers=headers, data=data, files=files, timeout=60)

                response.raise_for_status()
                result = response.text.strip()

                return jsonify({
                    "success": True,
                    "text": result,
                    "language": lang
                })

            finally:
                # 清理临时文件
                try:
                    os.unlink(temp_path)
                except:
                    pass

        except Exception as e:
            return jsonify({"error": str(e)}), 500
