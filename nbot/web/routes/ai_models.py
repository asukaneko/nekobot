import uuid
from datetime import datetime

from flask import jsonify, request


def register_ai_model_routes(app, server):
    @app.route("/api/ai-models")
    def get_ai_models():
        models = []
        for m in server.ai_models:
            model_copy = m.copy()
            if "api_key" in model_copy:
                model_copy["api_key"] = "********" if model_copy["api_key"] else ""
            models.append(model_copy)
        return jsonify({"models": models, "active_model_id": server.active_model_id})

    @app.route("/api/ai-models", methods=["POST"])
    def create_ai_model():
        data = request.json or {}
        model = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", "新模型"),
            "provider": data.get("provider", "custom"),
            "api_key": data.get("api_key", ""),
            "base_url": data.get("base_url", ""),
            "model": data.get("model", ""),
            "enabled": data.get("enabled", True),
            "temperature": data.get("temperature", 0.7),
            "max_tokens": data.get("max_tokens", 2000),
            "top_p": data.get("top_p", 0.9),
            "frequency_penalty": data.get("frequency_penalty", 0),
            "presence_penalty": data.get("presence_penalty", 0),
            "system_prompt": data.get("system_prompt", ""),
            "timeout": data.get("timeout", 60),
            "retry_count": data.get("retry_count", 3),
            "stream": data.get("stream", True),
            "enable_memory": data.get("enable_memory", True),
            "image_model": data.get("image_model", ""),
            "search_api_key": data.get("search_api_key", ""),
            "embedding_model": data.get("embedding_model", ""),
            "max_context_length": data.get("max_context_length", 8000),
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }
        server.ai_models.append(model)
        server._save_data("ai_models")
        return jsonify({"success": True, "model": model})

    @app.route("/api/ai-models/<model_id>", methods=["PUT"])
    def update_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] == model_id:
                data = request.json or {}
                model["name"] = data.get("name", model["name"])
                model["provider"] = data.get("provider", model["provider"])
                if data.get("api_key") and data["api_key"] != "********":
                    model["api_key"] = data["api_key"]
                model["base_url"] = data.get("base_url", model["base_url"])
                model["model"] = data.get("model", model["model"])
                model["enabled"] = data.get("enabled", model.get("enabled", True))
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
                model["updated_at"] = datetime.now().isoformat()
                server._save_data("ai_models")
                return jsonify({"success": True, "model": model})
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
        if server._apply_ai_model(model_id):
            return jsonify({"success": True, "message": "Model applied successfully"})
        return jsonify({"error": "Failed to apply model"}), 400

    @app.route("/api/ai-models/<model_id>/toggle", methods=["POST"])
    def toggle_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] == model_id:
                model["enabled"] = not model.get("enabled", True)
                server._save_data("ai_models")
                return jsonify({"success": True, "enabled": model["enabled"]})
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/ai-models/<model_id>/clone", methods=["POST"])
    def clone_ai_model(model_id):
        for model in server.ai_models:
            if model["id"] == model_id:
                cloned = model.copy()
                cloned["id"] = str(uuid.uuid4())
                cloned["name"] = f"{model['name']} (副本)"
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
            if model["id"] == model_id:
                api_key = model.get("api_key", "")
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

                    url_base = base_url.rstrip("/")
                    if "/chat/completions" in url_base or "/chatcompletion" in url_base:
                        url = url_base
                    else:
                        url = f"{url_base}/chat/completions"

                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }
                    payload = {
                        "model": model_name,
                        "messages": [{"role": "user", "content": "Hello"}],
                        "max_tokens": 10,
                    }
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
        return jsonify({"error": "Model not found"}), 404
