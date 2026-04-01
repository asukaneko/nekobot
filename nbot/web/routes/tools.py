import logging
import uuid

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_tool_routes(app, server):
    @app.route("/api/tools")
    def get_tools():
        seen_names = set()
        unique_tools = []

        for t in server.tools_config:
            name = t.get("name", "")
            if name not in seen_names:
                seen_names.add(name)
                unique_tools.append(t)

        try:
            from nbot.services.tools import TOOL_DEFINITIONS, WORKSPACE_TOOL_DEFINITIONS

            for tool_def in TOOL_DEFINITIONS + WORKSPACE_TOOL_DEFINITIONS:
                func = tool_def.get("function", {})
                name = func.get("name", "")
                if name and name not in seen_names:
                    unique_tools.append(
                        {
                            "id": f"_builtin_{name}",
                            "name": name,
                            "description": func.get("description", ""),
                            "enabled": True,
                            "parameters": func.get("parameters", {}),
                            "_builtin": True,
                        }
                    )
                    seen_names.add(name)
        except Exception as e:
            _log.error(f"Failed to load built-in tools: {e}")

        try:
            from nbot.services.tool_registry import (
                get_all_tool_definitions as get_registered_tools,
            )

            registered_tools = get_registered_tools()
            for tool_def in registered_tools:
                func = tool_def.get("function", {})
                name = func.get("name", "")
                if name and name not in seen_names:
                    unique_tools.append(
                        {
                            "id": f"_builtin_{name}",
                            "name": name,
                            "description": func.get("description", ""),
                            "enabled": True,
                            "parameters": func.get("parameters", {}),
                            "_builtin": True,
                        }
                    )
                    seen_names.add(name)
        except Exception as e:
            _log.error(f"Failed to load registered tools: {e}")

        return jsonify(unique_tools)

    @app.route("/api/tools", methods=["POST"])
    def create_tool():
        data = request.json or {}
        tool = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", ""),
            "description": data.get("description", ""),
            "enabled": data.get("enabled", True),
            "parameters": data.get("parameters", {}),
        }
        server.tools_config.append(tool)
        server._save_data("tools")
        return jsonify({"success": True, "tool": tool})

    @app.route("/api/tools/<tool_id>", methods=["PUT"])
    def update_tool(tool_id):
        data = request.json or {}
        for tool in server.tools_config:
            if tool["id"] == tool_id:
                tool["name"] = data.get("name", tool["name"])
                tool["description"] = data.get("description", tool["description"])
                tool["enabled"] = data.get("enabled", tool["enabled"])
                tool["parameters"] = data.get("parameters", tool["parameters"])
                server._save_data("tools")
                return jsonify({"success": True, "tool": tool})
        return jsonify({"error": "Tool not found"}), 404

    @app.route("/api/tools/<tool_id>", methods=["DELETE"])
    def delete_tool(tool_id):
        if tool_id.startswith("_builtin_"):
            return jsonify({"error": "Cannot delete built-in tool"}), 400
        server.tools_config = [t for t in server.tools_config if t["id"] != tool_id]
        server._save_data("tools")
        return jsonify({"success": True})

    @app.route("/api/tools/<tool_id>/toggle", methods=["POST"])
    def toggle_tool(tool_id):
        if tool_id.startswith("_builtin_"):
            return jsonify({"error": "Cannot toggle built-in tool"}), 400
        for tool in server.tools_config:
            if tool["id"] == tool_id:
                tool["enabled"] = not tool.get("enabled", True)
                server._save_data("tools")
                return jsonify({"success": True, "enabled": tool["enabled"]})
        return jsonify({"error": "Tool not found"}), 404
