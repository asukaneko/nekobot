import logging

from flask import jsonify, request

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

        if server.ai_api_key and server.ai_base_url:
            try:
                import configparser

                from nbot.services.ai import AIClient

                config = configparser.ConfigParser()
                config.read("config.ini", encoding="utf-8")

                server.ai_client = AIClient(
                    api_key=server.ai_api_key,
                    base_url=server.ai_base_url,
                    model=server.ai_model,
                    pic_model=config.get("pic", "model", fallback=""),
                    search_api_key=config.get("search", "api_key", fallback=""),
                    search_api_url=config.get("search", "api_url", fallback=""),
                    video_api=config.get("video", "api_key", fallback=""),
                    silicon_api_key=config.get(
                        "ApiKey", "silicon_api_key", fallback=""
                    ),
                )
            except Exception as e:
                _log.error(f"Failed to reinitialize AI client: {e}")

        server._save_data("ai_config")
        return jsonify({"success": True})

    @app.route("/api/ai-config/test", methods=["POST"])
    def test_ai_config():
        data = request.json or {}

        api_key = data.get("api_key", "")
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
                "model": model,
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
