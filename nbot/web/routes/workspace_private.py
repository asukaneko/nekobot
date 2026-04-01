import os
import re

from flask import jsonify, request, send_from_directory


def register_workspace_private_routes(app, server):
    @app.route("/api/sessions/<session_id>/workspace/files", methods=["GET"])
    def get_workspace_files(session_id):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        path = request.args.get("path", "")
        result = server.workspace_manager.list_files(session_id, path)
        return jsonify(result)

    @app.route("/api/sessions/<session_id>/workspace/upload", methods=["POST"])
    def upload_workspace_file(session_id):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        if session_id not in server.sessions:
            return jsonify({"error": "Session not found"}), 404

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if not file.filename:
            return jsonify({"error": "Empty filename"}), 400

        file_data = file.read()
        session_type = server.sessions[session_id].get("type", "web")
        result = server.workspace_manager.save_uploaded_file(
            session_id, file_data, file.filename, session_type
        )

        if result.get("success"):
            server.socketio.emit(
                "workspace_file_uploaded",
                {
                    "session_id": session_id,
                    "filename": result["filename"],
                    "size": result["size"],
                },
                room=session_id,
            )

        return jsonify(result)

    @app.route(
        "/api/sessions/<session_id>/workspace/files/<path:filename>",
        methods=["GET"],
    )
    def download_workspace_file(session_id, filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        file_path = server.workspace_manager.get_file_path(session_id, filename)
        if not file_path:
            return jsonify({"error": "File not found"}), 404

        directory = os.path.dirname(file_path)
        fname = os.path.basename(file_path)
        response = send_from_directory(directory, fname, as_attachment=True)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
        return response

    @app.route(
        "/api/sessions/<session_id>/workspace/files/<path:filename>",
        methods=["DELETE"],
    )
    def delete_workspace_file(session_id, filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        result = server.workspace_manager.delete_file(session_id, filename)
        return jsonify(result)

    @app.route("/api/sessions/<session_id>/workspace/folders", methods=["POST"])
    def create_workspace_folder(session_id):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        data = request.json or {}
        folder_name = data.get("name", "").strip()
        path = data.get("path", "")

        if not folder_name:
            return jsonify({"success": False, "error": "Folder name is required"}), 400

        if not re.match(r"^[\\w\\-\\. ]+$", folder_name):
            return jsonify({"success": False, "error": "Invalid folder name"}), 400

        ws_path = server.workspace_manager.get_workspace(session_id)
        if not ws_path:
            return jsonify({"success": False, "error": "Workspace not found"}), 404

        folder_path = (
            os.path.join(ws_path, path, folder_name)
            if path
            else os.path.join(ws_path, folder_name)
        )

        try:
            if os.path.exists(folder_path):
                return jsonify({"success": False, "error": "Folder already exists"}), 400

            os.makedirs(folder_path, exist_ok=True)
            return jsonify(
                {
                    "success": True,
                    "name": folder_name,
                    "path": path + "/" + folder_name if path else folder_name,
                }
            )
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route(
        "/api/sessions/<session_id>/workspace/files/<path:filename>/move",
        methods=["POST"],
    )
    def move_workspace_file(session_id, filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"success": False, "error": "Workspace not available"}), 503

        data = request.json or {}
        target_path = data.get("target", "")

        result = server.workspace_manager.move_file(session_id, filename, target_path)
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400

    @app.route(
        "/api/sessions/<session_id>/workspace/files/<path:filename>/move-to-shared",
        methods=["POST"],
    )
    def move_file_to_shared(session_id, filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        data = request.json or {}
        target_path = data.get("target", "")

        result = server.workspace_manager.move_to_shared(
            session_id, filename, target_path
        )
        if result.get("success"):
            return jsonify(result)
        return jsonify(result), 400

    @app.route(
        "/api/sessions/<session_id>/workspace/files/<path:filename>/preview",
        methods=["GET"],
    )
    def preview_workspace_file(session_id, filename):
        if not server.WORKSPACE_AVAILABLE:
            return jsonify({"error": "Workspace not available"}), 503

        file_path = server.workspace_manager.get_file_path(session_id, filename)
        if not file_path:
            return jsonify({"error": "File not found"}), 404

        ext = os.path.splitext(filename.lower())[1]
        image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"]
        if ext in image_exts:
            return jsonify(
                {
                    "success": True,
                    "type": "image",
                    "url": f"/api/sessions/{session_id}/workspace/files/{filename}",
                }
            )

        from nbot.core.file_parser import FileParser

        parse_result = FileParser.parse_file(file_path, filename, max_chars=50000)
        if not parse_result or not parse_result.get("success"):
            return jsonify(
                {
                    "success": False,
                    "error": parse_result.get("error", "Failed to parse file")
                    if parse_result
                    else "File not found",
                }
            ), 400

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
