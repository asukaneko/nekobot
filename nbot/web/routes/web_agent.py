from flask import jsonify, request

from nbot.web.agent_tools import (
    execute_web_agent_tool,
    get_web_agent_tools,
    run_web_agent_turn,
)


def register_web_agent_routes(app, server):
    @app.route("/api/web-agent/tools")
    def list_web_agent_tools():
        return jsonify({"success": True, "tools": get_web_agent_tools()})

    @app.route("/api/web-agent/execute", methods=["POST"])
    def execute_tool():
        data = request.json or {}
        result = execute_web_agent_tool(
            server,
            data.get("tool", ""),
            data.get("arguments") or {},
            confirm=bool(data.get("confirm")),
        )
        status = result.pop("status", 200)
        return jsonify(result), status

    @app.route("/api/web-agent/chat", methods=["POST"])
    def web_agent_chat():
        data = request.json or {}
        result = run_web_agent_turn(
            server,
            data.get("message", ""),
            allow_write=bool(data.get("allow_write")),
        )
        return jsonify(result)
