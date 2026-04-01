from flask import jsonify, request


def register_config_legacy_routes(app, server):
    @app.route("/api/config")
    def get_config():
        try:
            with open("config.ini", "r", encoding="utf-8") as f:
                content = f.read()
            return jsonify({"content": content})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/config", methods=["POST"])
    def save_config():
        data = request.json or {}
        try:
            with open("config.ini", "w", encoding="utf-8") as f:
                f.write(data.get("content", ""))
            return jsonify({"success": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
