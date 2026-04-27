import logging

from flask import jsonify, request

from nbot.core import build_chat_completion_payload, resolve_chat_completion_url
from nbot.web.utils.config_loader import (
    resolve_runtime_api_key,
    get_chat_model_config,
    get_vision_model_config,
    get_video_model_config,
    get_tts_model_config,
    get_stt_model_config,
    get_embedding_model_config,
)

_log = logging.getLogger(__name__)


def register_ai_config_routes(app, server):
    @app.route("/api/ai-config")
    def get_ai_config():
        config = server.ai_config.copy()
        config["api_key"] = "********" if config.get("api_key") else ""
        config["model"] = server.ai_model or config.get("model", "gpt-4")
        config["base_url"] = server.ai_base_url or config.get("base_url", "")
        return jsonify(config)

    @app.route("/api/ai-config", methods=["PUT"])
    def update_ai_config():
        data = request.json or {}

        if data.get("provider"):
            server.ai_config["provider"] = data["provider"]
        if data.get("provider_type"):
            server.ai_config["provider_type"] = data["provider_type"]
        if data.get("api_key") and data["api_key"] != "********":
            server.ai_config["api_key"] = data["api_key"]
            server.ai_api_key = data["api_key"]
        if data.get("base_url") is not None:
            server.ai_config["base_url"] = data["base_url"]
            server.ai_base_url = data["base_url"]
        if data.get("model"):
            server.ai_config["model"] = data["model"]
            server.ai_model = data["model"]
        if data.get("temperature") is not None:
            server.ai_config["temperature"] = data["temperature"]
        if data.get("max_tokens") is not None:
            server.ai_config["max_tokens"] = data["max_tokens"]
        if data.get("top_p") is not None:
            server.ai_config["top_p"] = data["top_p"]
        if data.get("frequency_penalty") is not None:
            server.ai_config["frequency_penalty"] = data["frequency_penalty"]
        if data.get("presence_penalty") is not None:
            server.ai_config["presence_penalty"] = data["presence_penalty"]
        if data.get("system_prompt") is not None:
            server.ai_config["system_prompt"] = data["system_prompt"]
        if data.get("timeout") is not None:
            server.ai_config["timeout"] = data["timeout"]
        if data.get("retry_count") is not None:
            server.ai_config["retry_count"] = data["retry_count"]
        if data.get("stream") is not None:
            server.ai_config["stream"] = data["stream"]
        if data.get("enable_memory") is not None:
            server.ai_config["enable_memory"] = data["enable_memory"]
        if data.get("image_model") is not None:
            server.ai_config["image_model"] = data["image_model"]
        if data.get("search_api_key") is not None:
            server.ai_config["search_api_key"] = data["search_api_key"]
        if data.get("embedding_model") is not None:
            server.ai_config["embedding_model"] = data["embedding_model"]
        if data.get("max_context_length") is not None:
            server.ai_config["max_context_length"] = data["max_context_length"]
        if data.get("supports_tools") is not None:
            server.ai_config["supports_tools"] = data["supports_tools"]
        if data.get("supports_reasoning") is not None:
            server.ai_config["supports_reasoning"] = data["supports_reasoning"]
        if data.get("supports_stream") is not None:
            server.ai_config["supports_stream"] = data["supports_stream"]

        if server.ai_api_key and server.ai_base_url and not server._initialize_ai_client():
            _log.error("Failed to reinitialize AI client")

        server._save_data("ai_config")
        return jsonify({"success": True})

    @app.route("/api/ai-config/test", methods=["POST"])
    def test_ai_config():
        data = request.json or {}

        provider_type = data.get(
            "provider_type",
            data.get("provider", "openai_compatible"),
        )
        api_key = resolve_runtime_api_key(data.get("api_key", ""), provider_type)
        base_url = data.get("base_url", "")
        model = data.get("model", "")

        if not api_key:
            return jsonify({"success": False, "message": "API Key is required"})
        if not base_url:
            return jsonify({"success": False, "message": "Base URL is required"})
        if not model:
            return jsonify({"success": False, "message": "Model is required"})

        try:
            import requests

            url = resolve_chat_completion_url(
                base_url,
                model=model,
                provider_type=provider_type,
            )

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = build_chat_completion_payload(
                model,
                [{"role": "user", "content": "Hello"}],
                base_url=base_url,
                provider_type=provider_type,
                extra_body={"max_tokens": 10},
            )

            resp = requests.post(url, json=payload, headers=headers, timeout=30)
            resp.raise_for_status()
            return jsonify({"success": True, "message": "Connection successful"})
        except requests.exceptions.Timeout:
            return jsonify({"success": False, "message": "Connection timed out"})
        except requests.exceptions.ConnectionError:
            return jsonify({"success": False, "message": "Connection failed"})
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP error: {e.response.status_code}"
            try:
                error_data = e.response.json()
                if "error" in error_data:
                    error_msg += f" - {error_data['error'].get('message', 'Unknown error')}"
            except Exception:
                pass
            return jsonify({"success": False, "message": error_msg})
        except Exception as e:
            return jsonify({"success": False, "message": f"Test failed: {str(e)}"})

    # ========== 按用途获取模型配置API ==========
    
    @app.route("/api/ai-config/by-purpose/<purpose>")
    def get_config_by_purpose(purpose):
        """获取指定用途的模型配置"""
        purpose_map = {
            "chat": get_chat_model_config,
            "vision": get_vision_model_config,
            "video": get_video_model_config,
            "tts": get_tts_model_config,
            "stt": get_stt_model_config,
            "embedding": get_embedding_model_config,
        }
        
        if purpose not in purpose_map:
            return jsonify({"error": "Invalid purpose"}), 400
        
        try:
            config = purpose_map[purpose]()
            if config:
                # 隐藏API Key
                config_copy = config.copy()
                if "api_key" in config_copy:
                    config_copy["api_key"] = "********" if config_copy["api_key"] else ""
                return jsonify({
                    "success": True,
                    "purpose": purpose,
                    "config": config_copy
                })
            else:
                return jsonify({
                    "success": False,
                    "message": f"No active model configured for purpose: {purpose}"
                }), 404
        except Exception as e:
            _log.error(f"Error getting config for purpose {purpose}: {e}")
            return jsonify({"error": str(e)}), 500
    
    @app.route("/api/ai-config/all-purposes")
    def get_all_purpose_configs():
        """获取所有用途的模型配置"""
        try:
            configs = {
                "chat": get_chat_model_config(),
                "vision": get_vision_model_config(),
                "video": get_video_model_config(),
                "tts": get_tts_model_config(),
                "stt": get_stt_model_config(),
                "embedding": get_embedding_model_config(),
            }
            
            # 隐藏API Key
            for purpose, config in configs.items():
                if config and "api_key" in config:
                    config["api_key"] = "********" if config["api_key"] else ""
            
            return jsonify({
                "success": True,
                "configs": configs
            })
        except Exception as e:
            _log.error(f"Error getting all purpose configs: {e}")
            return jsonify({"error": str(e)}), 500
