import os
from urllib.parse import unquote

from flask import jsonify, request


def register_workspace_shared_routes(app, server):
    def _resolve_shared_path(*parts):
        base = os.path.abspath(server.workspace_manager.get_shared_workspace())
        target = os.path.abspath(os.path.join(base, *[p for p in parts if p]))
        try:
            if os.path.commonpath([base, target]) != base:
                return None
        except ValueError:
            return None
        return target

    @app.route("/api/workspace/shared/files", methods=["GET"])
    def get_shared_workspace_files():
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        path = request.args.get("path", "")
        result = server.workspace_manager.list_shared_files(path)
        return jsonify(result)

    @app.route("/api/workspace/shared/files/<path:filename>", methods=["GET"])
    def get_shared_workspace_file(filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        filename = unquote(filename)
        file_path = _resolve_shared_path(filename)
        if not file_path:
            return jsonify({"error": "Invalid path"}), 400
        if not os.path.exists(file_path):
            return jsonify({"error": "File not found"}), 404

        ext = os.path.splitext(filename.lower())[1]
        image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"]
        if ext in image_exts:
            return jsonify(
                {
                    "success": True,
                    "type": "image",
                    "filename": filename,
                    "url": f"/api/workspace/shared/files/{filename}",
                }
            )

        from nbot.core.file_parser import FileParser

        parse_result = FileParser.parse_file(file_path, filename, max_chars=50000)
        if parse_result and parse_result.get("success"):
            return jsonify(
                {
                    "success": True,
                    "type": parse_result.get("type", "text"),
                    "content": parse_result.get("content", ""),
                    "filename": filename,
                    "extracted_length": parse_result.get("extracted_length", 0),
                    "original_length": parse_result.get("original_length", 0),
                    "truncated": parse_result.get("truncated", False),
                }
            )

        error_msg = (
            parse_result.get("error", "Failed to parse file")
            if parse_result
            else "File not found"
        )
        return jsonify({"success": False, "error": error_msg}), 400

    @app.route("/api/workspace/shared/files/<path:filename>", methods=["DELETE"])
    def delete_shared_workspace_file(filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        result = server.workspace_manager.delete_shared_file(filename)
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400

    @app.route("/api/workspace/shared/folders", methods=["POST"])
    def create_shared_workspace_folder():
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        data = request.json or {}
        folder_name = data.get("name", "").strip()
        path = data.get("path", "")

        result = server.workspace_manager.create_shared_folder(folder_name, path)
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400

    @app.route("/api/workspace/shared/files/<path:filename>/move", methods=["POST"])
    def move_shared_workspace_file(filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        data = request.json or {}
        target_path = data.get("target", "")

        result = server.workspace_manager.move_shared_file(filename, target_path)
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400

    @app.route(
        "/api/workspace/shared/files/<path:filename>/move-to-private",
        methods=["POST"],
    )
    def move_shared_file_to_private(filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        data = request.json or {}
        session_id = data.get("session_id")
        target_path = data.get("target", "")

        if not session_id:
            return jsonify({"error": "session_id is required"}), 400

        result = server.workspace_manager.move_from_shared(
            session_id, filename, target_path
        )
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400
