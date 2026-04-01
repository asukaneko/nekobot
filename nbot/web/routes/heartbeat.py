import os

from flask import jsonify, request


def register_heartbeat_routes(app, server):
    @app.route("/api/heartbeat")
    def get_heartbeat():
        return jsonify(server.heartbeat_config)

    @app.route("/api/heartbeat", methods=["PUT"])
    def update_heartbeat():
        data = request.json or {}

        enabled = data.get("enabled", server.heartbeat_config.get("enabled", False))
        interval_minutes = data.get(
            "interval_minutes", server.heartbeat_config.get("interval_minutes", 60)
        )
        content_file = data.get(
            "content_file", server.heartbeat_config.get("content_file", "heartbeat.md")
        )
        target_session_id = data.get(
            "target_session_id", server.heartbeat_config.get("target_session_id", "")
        )
        targets = data.get("targets", server.heartbeat_config.get("targets", []))

        server.heartbeat_config = {
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "content_file": content_file,
            "target_session_id": target_session_id,
            "targets": targets,
            "last_run": server.heartbeat_config.get("last_run"),
            "next_run": server.heartbeat_config.get("next_run"),
        }

        if enabled:
            server._start_heartbeat_job(interval_minutes)
        else:
            server._stop_heartbeat_job()

        server._save_data("heartbeat")
        return jsonify({"success": True, "config": server.heartbeat_config})

    @app.route("/api/heartbeat/run", methods=["POST"])
    def run_heartbeat():
        try:
            import asyncio

            asyncio.run(server._execute_heartbeat(force=True))
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/heartbeat/content", methods=["GET"])
    def get_heartbeat_content():
        content_file = request.args.get("file", "heartbeat.md")
        content = server._load_heartbeat_content(content_file)
        return jsonify({"content": content, "file": content_file})

    @app.route("/api/heartbeat/content", methods=["PUT"])
    def save_heartbeat_content():
        data = request.json or {}
        content = data.get("content", "")
        content_file = data.get("file", "heartbeat.md")

        save_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "resources", content_file
        )

        try:
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(content)
            return jsonify({"success": True, "path": save_path})
        except Exception:
            try:
                save_path = os.path.join(os.getcwd(), "resources", content_file)
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return jsonify({"success": True, "path": save_path})
            except Exception as e2:
                return jsonify({"success": False, "error": f"保存 heartbeat 文件失败: {e2}"}), 500
