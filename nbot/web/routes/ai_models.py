import uuid
from datetime import datetime

from flask import jsonify, request
from nbot.web.utils.config_loader import resolve_runtime_api_key

# 模型用途类型定义
MODEL_PURPOSES = {
    "chat": {"name": "对话模型", "icon": "💬", "description": "用于日常对话和问答"},
    "vision": {"name": "图片理解模型", "icon": "🖼️", "description": "用于识别和理解图片内容"},
    "video": {"name": "视频理解模型", "icon": "🎬", "description": "用于分析视频内容"},
    "tts": {"name": "TTS语音合成", "icon": "🔊", "description": "用于文字转语音"},
    "stt": {"name": "STT语音识别", "icon": "🎤", "description": "用于语音转文字"},
    "embedding": {"name": "向量嵌入模型", "icon": "📊", "description": "用于知识库和语义搜索"}
}

# 各用途的默认配置
DEFAULT_PURPOSE_CONFIGS = {
    "chat": {
        "temperature": 0.7,
        "max_tokens": 2000,
        "top_p": 0.9,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_stream": True,
        "system_prompt": ""
    },
    "vision": {
        "temperature": 0.5,
        "max_tokens": 1000,
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_stream": True,
        "system_prompt": "请详细描述这张图片的内容。"
    },
    "video": {
        "temperature": 0.5,
        "max_tokens": 1500,
        "supports_tools": False,
        "supports_reasoning": False,
        "supports_stream": True,
        "system_prompt": "请分析这个视频的内容。"
    },
    "tts": {
        "voice": "default",
        "speed": 1.0,
        "pitch": 1.0,
        "volume": 1.0
    },
    "stt": {
        "language": "zh",
        "model": "whisper-1"
    },
    "embedding": {
        "model": "text-embedding-3-small",
        "dimensions": 1536
    }
}


def register_ai_model_routes(app, server):
    @app.route("/api/ai-models")
    def get_ai_models():
        models = []
        for model in server.ai_models:
            model_copy = model.copy()
            if "api_key" in model_copy:
                model_copy["api_key"] = "********" if model_copy["api_key"] else ""
            models.append(model_copy)
        return jsonify({"models": models, "active_model_id": server.active_model_id})

    @app.route("/api/ai-models", methods=["POST"])
    def create_ai_model():
        data = request.json or {}
        now = datetime.now().isoformat()
        
        # 获取模型用途，默认为对话模型
        purpose = data.get("purpose", "chat")
        # 获取该用途的默认配置
        default_config = DEFAULT_PURPOSE_CONFIGS.get(purpose, DEFAULT_PURPOSE_CONFIGS["chat"])
        
        model = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", f"新{purpose}配置"),
            "purpose": purpose,  # 新增：模型用途
            "provider": data.get("provider", "custom"),
            "provider_type": data.get(
                "provider_type",
                data.get("provider", "openai_compatible"),
            ),
            "api_key": data.get("api_key", ""),
            "base_url": data.get("base_url", ""),
            "model": data.get("model", ""),
            "enabled": data.get("enabled", True),
            "supports_tools": data.get("supports_tools", default_config.get("supports_tools", True)),
            "supports_reasoning": data.get("supports_reasoning", default_config.get("supports_reasoning", True)),
            "supports_stream": data.get("supports_stream", default_config.get("supports_stream", True)),
            "temperature": data.get("temperature", default_config.get("temperature", 0.7)),
            "max_tokens": data.get("max_tokens", default_config.get("max_tokens", 2000)),
            "top_p": data.get("top_p", default_config.get("top_p", 0.9)),
            "frequency_penalty": data.get("frequency_penalty", 0),
            "presence_penalty": data.get("presence_penalty", 0),
            "system_prompt": data.get("system_prompt", default_config.get("system_prompt", "")),
            "timeout": data.get("timeout", 60),
            "retry_count": data.get("retry_count", 3),
            "stream": data.get("stream", True),
            "enable_memory": data.get("enable_memory", True),
            "image_model": data.get("image_model", ""),
            "search_api_key": data.get("search_api_key", ""),
            "embedding_model": data.get("embedding_model", default_config.get("model", "") if purpose == "embedding" else ""),
            "max_context_length": data.get("max_context_length", 8000),
            # TTS/STT特有配置
            "voice": data.get("voice", default_config.get("voice", "default")),
            "speed": data.get("speed", default_config.get("speed", 1.0)),
            "pitch": data.get("pitch", default_config.get("pitch", 1.0)),
            "volume": data.get("volume", default_config.get("volume", 1.0)),
            "language": data.get("language", default_config.get("language", "zh")),
            "dimensions": data.get("dimensions", default_config.get("dimensions", 1536)),
            "created_at": now,
            "updated_at": now,
        }
        server.ai_models.append(model)
        server._save_data("ai_models")
        return jsonify({"success": True, "model": model})

    @app.route("/api/ai-models/<model_id>", methods=["PUT"])
    def update_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue

            data = request.json or {}
            model["name"] = data.get("name", model["name"])
            # 支持修改模型用途
            if "purpose" in data:
                old_purpose = model.get("purpose", "chat")
                new_purpose = data["purpose"]
                if old_purpose != new_purpose:
                    # 用途变更时，应用新用途的默认配置
                    model["purpose"] = new_purpose
                    default_config = DEFAULT_PURPOSE_CONFIGS.get(new_purpose, DEFAULT_PURPOSE_CONFIGS["chat"])
                    model["supports_tools"] = default_config.get("supports_tools", True)
                    model["supports_reasoning"] = default_config.get("supports_reasoning", True)
                    model["supports_stream"] = default_config.get("supports_stream", True)
                    model["temperature"] = default_config.get("temperature", 0.7)
                    model["max_tokens"] = default_config.get("max_tokens", 2000)
                    model["top_p"] = default_config.get("top_p", 0.9)
                    model["system_prompt"] = default_config.get("system_prompt", "")
            
            model["provider"] = data.get("provider", model["provider"])
            model["provider_type"] = data.get(
                "provider_type",
                model.get("provider_type", model.get("provider", "openai_compatible")),
            )
            if data.get("api_key") and data["api_key"] != "********":
                model["api_key"] = data["api_key"]
            model["base_url"] = data.get("base_url", model["base_url"])
            model["model"] = data.get("model", model["model"])
            model["enabled"] = data.get("enabled", model.get("enabled", True))
            model["supports_tools"] = data.get(
                "supports_tools", model.get("supports_tools", True)
            )
            model["supports_reasoning"] = data.get(
                "supports_reasoning", model.get("supports_reasoning", True)
            )
            model["supports_stream"] = data.get(
                "supports_stream", model.get("supports_stream", True)
            )
            model["temperature"] = data.get("temperature", model.get("temperature", 0.7))
            model["max_tokens"] = data.get("max_tokens", model.get("max_tokens", 2000))
            model["top_p"] = data.get("top_p", model.get("top_p", 0.9))
            model["frequency_penalty"] = data.get(
                "frequency_penalty", model.get("frequency_penalty", 0)
            )
            model["presence_penalty"] = data.get(
                "presence_penalty", model.get("presence_penalty", 0)
            )
            model["system_prompt"] = data.get(
                "system_prompt", model.get("system_prompt", "")
            )
            model["timeout"] = data.get("timeout", model.get("timeout", 60))
            model["retry_count"] = data.get("retry_count", model.get("retry_count", 3))
            model["stream"] = data.get("stream", model.get("stream", True))
            model["enable_memory"] = data.get(
                "enable_memory", model.get("enable_memory", True)
            )
            model["image_model"] = data.get("image_model", model.get("image_model", ""))
            model["search_api_key"] = data.get(
                "search_api_key", model.get("search_api_key", "")
            )
            model["embedding_model"] = data.get(
                "embedding_model", model.get("embedding_model", "")
            )
            model["max_context_length"] = data.get(
                "max_context_length", model.get("max_context_length", 8000)
            )
            # TTS/STT/Embedding特有配置
            model["voice"] = data.get("voice", model.get("voice", "default"))
            model["speed"] = data.get("speed", model.get("speed", 1.0))
            model["pitch"] = data.get("pitch", model.get("pitch", 1.0))
            model["volume"] = data.get("volume", model.get("volume", 1.0))
            model["language"] = data.get("language", model.get("language", "zh"))
            model["dimensions"] = data.get("dimensions", model.get("dimensions", 1536))
            model["updated_at"] = datetime.now().isoformat()
            server._save_data("ai_models")
            return jsonify({"success": True, "model": model})

        return jsonify({"error": "Model not found"}), 404

    # ========== 模型用途相关API ==========
    
    @app.route("/api/ai-models/purposes")
    def get_model_purposes():
        """获取所有模型用途类型列表"""
        return jsonify({
            "success": True,
            "purposes": MODEL_PURPOSES,
            "default_configs": DEFAULT_PURPOSE_CONFIGS
        })
    
    @app.route("/api/ai-models/by-purpose/<purpose>")
    def get_models_by_purpose(purpose):
        """按用途获取模型配置列表"""
        if purpose not in MODEL_PURPOSES:
            return jsonify({"error": "Invalid purpose"}), 400
        
        models = []
        for model in server.ai_models:
            if model.get("purpose", "chat") == purpose:
                model_copy = model.copy()
                if "api_key" in model_copy:
                    model_copy["api_key"] = "********" if model_copy["api_key"] else ""
                models.append(model_copy)
        
        return jsonify({
            "success": True,
            "purpose": purpose,
            "purpose_info": MODEL_PURPOSES.get(purpose),
            "models": models
        })
    
    @app.route("/api/ai-models/active-by-purpose")
    def get_active_models_by_purpose():
        """获取当前各用途的活跃模型配置"""
        active_models = {}
        
        for purpose in MODEL_PURPOSES.keys():
            # 首先检查是否有明确设置的活跃模型ID
            active_model_id = server.active_models_by_purpose.get(purpose)
            active_model = None
            
            if active_model_id:
                # 查找指定ID的模型
                for model in server.ai_models:
                    if model.get("id") == active_model_id and model.get("enabled", True):
                        active_model = model.copy()
                        if "api_key" in active_model:
                            active_model["api_key"] = "********" if active_model["api_key"] else ""
                        break
            
            # 如果没有找到，使用第一个可用的该用途模型
            if not active_model:
                for model in server.ai_models:
                    if model.get("purpose", "chat") == purpose and model.get("enabled", True):
                        active_model = model.copy()
                        if "api_key" in active_model:
                            active_model["api_key"] = "********" if active_model["api_key"] else ""
                        # 自动设置该用途的活跃模型
                        server.active_models_by_purpose[purpose] = model.get("id")
                        break
            
            active_models[purpose] = {
                "purpose_info": MODEL_PURPOSES.get(purpose),
                "model": active_model,
                "has_config": active_model is not None,
                "active_model_id": active_model.get("id") if active_model else None
            }
        
        return jsonify({
            "success": True,
            "active_models": active_models
        })
    
    @app.route("/api/ai-models/<model_id>/set-purpose", methods=["POST"])
    def set_model_purpose(model_id):
        """设置模型用途"""
        data = request.json or {}
        new_purpose = data.get("purpose")
        
        if not new_purpose or new_purpose not in MODEL_PURPOSES:
            return jsonify({"error": "Invalid or missing purpose"}), 400
        
        for model in server.ai_models:
            if model["id"] != model_id:
                continue
            
            old_purpose = model.get("purpose", "chat")
            if old_purpose != new_purpose:
                model["purpose"] = new_purpose
                # 应用新用途的默认配置
                default_config = DEFAULT_PURPOSE_CONFIGS.get(new_purpose, DEFAULT_PURPOSE_CONFIGS["chat"])
                model["supports_tools"] = default_config.get("supports_tools", True)
                model["supports_reasoning"] = default_config.get("supports_reasoning", True)
                model["supports_stream"] = default_config.get("supports_stream", True)
                model["temperature"] = default_config.get("temperature", 0.7)
                model["max_tokens"] = default_config.get("max_tokens", 2000)
                model["top_p"] = default_config.get("top_p", 0.9)
                model["system_prompt"] = default_config.get("system_prompt", "")
                model["updated_at"] = datetime.now().isoformat()
                server._save_data("ai_models")
            
            model_copy = model.copy()
            if "api_key" in model_copy:
                model_copy["api_key"] = "********" if model_copy["api_key"] else ""
            
            return jsonify({
                "success": True,
                "message": f"Model purpose changed from {old_purpose} to {new_purpose}",
                "model": model_copy
            })
        
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/<model_id>", methods=["DELETE"])
    def delete_ai_model(model_id):
        if server.active_model_id == model_id:
            return jsonify({"error": "Cannot delete active model"}), 400
        server.ai_models = [m for m in server.ai_models if m["id"] != model_id]
        server._save_data("ai_models")
        return jsonify({"success": True})

    @app.route("/api/ai-models/<model_id>/apply", methods=["POST"])
    def apply_ai_model(model_id):
        """应用指定的AI模型配置
        
        请求体中可以指定purpose参数，如果不指定则自动从模型配置中获取
        """
        data = request.json or {}
        purpose = data.get("purpose")
        
        if server._apply_ai_model(model_id, purpose=purpose):
            # 获取应用的模型信息
            model = None
            for m in server.ai_models:
                if m["id"] == model_id:
                    model = m
                    break
            model_purpose = purpose or (model.get("purpose", "chat") if model else "chat")
            purpose_name = MODEL_PURPOSES.get(model_purpose, {}).get("name", model_purpose)
            return jsonify({
                "success": True, 
                "message": f"已应用 {purpose_name} 配置: {model.get('name', 'Unknown') if model else 'Unknown'}",
                "purpose": model_purpose,
                "model_id": model_id
            })
        return jsonify({"error": "Failed to apply model"}), 400

    @app.route("/api/ai-models/<model_id>/toggle", methods=["POST"])
    def toggle_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue
            model["enabled"] = not model.get("enabled", True)
            server._save_data("ai_models")
            return jsonify({"success": True, "enabled": model["enabled"]})
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/<model_id>/clone", methods=["POST"])
    def clone_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue
            cloned = model.copy()
            cloned["id"] = str(uuid.uuid4())
            cloned["name"] = f"{model['name']}（副本）"
            cloned["is_default"] = False
            cloned["created_at"] = datetime.now().isoformat()
            cloned["updated_at"] = datetime.now().isoformat()
            server.ai_models.append(cloned)
            server._save_data("ai_models")
            return jsonify({"success": True, "model": cloned})
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/<model_id>/test", methods=["POST"])
    def test_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] != model_id:
                continue

            provider_type = model.get(
                "provider_type", model.get("provider", "openai_compatible")
            )
            api_key = resolve_runtime_api_key(model.get("api_key", ""), provider_type)
            base_url = model.get("base_url", "")
            model_name = model.get("model", "")

            if not api_key:
                return jsonify({"success": False, "message": "API Key is required"})
            if not base_url:
                return jsonify({"success": False, "message": "Base URL is required"})
            if not model_name:
                return jsonify({"success": False, "message": "Model is required"})

            try:
                import requests
                from nbot.core import (
                    build_chat_completion_payload,
                    resolve_chat_completion_url,
                )

                url = resolve_chat_completion_url(
                    base_url,
                    model=model_name,
                    provider_type=provider_type,
                )

                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }
                payload = build_chat_completion_payload(
                    model_name,
                    [{"role": "user", "content": "Hello"}],
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
                        error_msg += (
                            f" - {error_data['error'].get('message', 'Unknown error')}"
                        )
                except Exception:
                    pass
                return jsonify({"success": False, "message": error_msg})
            except Exception as e:
                return jsonify({"success": False, "message": f"Test failed: {str(e)}"})

        return jsonify({"error": "Model not found"}), 404
