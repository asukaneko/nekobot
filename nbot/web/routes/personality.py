import logging
import os
import uuid
from datetime import datetime

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_personality_routes(app, server):
    @app.route("/api/personality")
    def get_personality():
        return jsonify(server.personality)

    @app.route("/api/personality", methods=["PUT"])
    def update_personality():
        data = request.json or {}
        server.personality["name"] = data.get("name", server.personality.get("name", ""))
        server.personality["prompt"] = data.get(
            "prompt", server.personality.get("prompt", "")
        )

        try:
            prompt_file = os.path.join(
                server.base_dir, "resources", "prompts", "neko.txt"
            )
            os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(server.personality["prompt"])
        except Exception as e:
            _log.error(f"Failed to save personality: {e}")

        return jsonify({"success": True, "personality": server.personality})

    @app.route("/api/personality/presets")
    def get_personality_presets():
        neko_prompt = server.personality.get("prompt", "")
        presets = [
            {
                "id": "1",
                "name": "猫娘助手",
                "icon": "cat",
                "description": "活泼可爱的猫娘助手",
                "prompt": neko_prompt,
            },
            {
                "id": "2",
                "name": "学习助手",
                "icon": "book",
                "description": "专注解释、总结和辅导学习问题",
                "prompt": "你是一个耐心、清晰、擅长教学的 AI 助手。",
            },
            {
                "id": "3",
                "name": "代码助手",
                "icon": "code",
                "description": "偏重编程、排错和工程实现",
                "prompt": "你是一个专业的软件开发助手，回答务实、准确、可执行。",
            },
            {
                "id": "4",
                "name": "创意写手",
                "icon": "pen",
                "description": "偏重文案、故事和创意表达",
                "prompt": "你是一个富有创意的写作助手，擅长构思、润色和扩展文本。",
            },
        ]
        return jsonify(presets)

    @app.route("/api/personality/custom-presets", methods=["GET"])
    def get_custom_personality_presets():
        return jsonify(server.custom_personality_presets)

    @app.route("/api/personality/custom-presets", methods=["POST"])
    def add_custom_personality_preset():
        data = request.json or {}
        preset = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "icon": data.get("icon", "spark"),
            "prompt": data.get("prompt", ""),
            "created_at": datetime.now().isoformat(),
        }
        server.custom_personality_presets.append(preset)
        server._save_data("custom_personality_presets")
        return jsonify(preset)

    @app.route("/api/personality/custom-presets/<preset_id>", methods=["DELETE"])
    def delete_custom_personality_preset(preset_id):
        server.custom_personality_presets = [
            p for p in server.custom_personality_presets if p["id"] != preset_id
        ]
        server._save_data("custom_personality_presets")
        return jsonify({"success": True})
