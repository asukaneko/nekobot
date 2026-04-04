import json
import os
import time

from flask import jsonify, request, send_from_directory

from nbot.core import WebSessionStore


def register_file_routes(app, server, workspace_available, workspace_manager):
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )

    max_file_size = 50 * 1024 * 1024
    preview_text_size_limit = 10 * 1024 * 1024

    @app.route("/static/files/<path:filename>")
    def serve_file(filename):
        files_dir = os.path.join(server.static_folder, "files")
        return send_from_directory(files_dir, filename, as_attachment=True)

    @app.route("/api/files/<path:safe_name>/preview", methods=["GET"])
    def preview_static_file(safe_name):
        import mimetypes
        from nbot.core.file_parser import FileParser

        files_dir = os.path.join(server.static_folder, "files")
        file_path = os.path.join(files_dir, safe_name)

        if not os.path.exists(file_path):
            return jsonify({"success": False, "error": "File not found"}), 404

        ext = os.path.splitext(safe_name.lower())[1]
        image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"]
        if ext in image_exts:
            return jsonify(
                {
                    "success": True,
                    "type": "image",
                    "url": f"/static/files/{safe_name}",
                    "download_url": f"/static/files/{safe_name}",
                    "safe_name": safe_name,
                }
            )

        frontend_render_exts = [
            ".pdf",
            ".pptx",
            ".ppt",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
        ]
        if ext in frontend_render_exts:
            return jsonify(
                {
                    "success": True,
                    "type": ext[1:],
                    "is_blob": True,
                    "url": f"/static/files/{safe_name}",
                    "download_url": f"/static/files/{safe_name}",
                    "safe_name": safe_name,
                }
            )

        parse_result = FileParser.parse_file(
            file_path,
            safe_name,
            max_chars=preview_text_size_limit,
        )
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
                "filename": safe_name,
                "url": f"/static/files/{safe_name}",
                "download_url": f"/static/files/{safe_name}",
                "safe_name": safe_name,
                "extracted_length": parse_result.get("extracted_length", 0),
                "original_length": parse_result.get("original_length", 0),
                "truncated": parse_result.get("truncated", False),
            }
        )

    @app.route("/static/<path:filename>")
    def serve_static(filename):
        return send_from_directory(server.static_folder, filename)

    @app.route("/api/upload", methods=["POST"])
    def upload_file():
        try:
            if "file" not in request.files:
                return jsonify({"error": "No file provided"}), 400

            file = request.files["file"]
            if file.filename == "":
                return jsonify({"error": "No file selected"}), 400

            file.seek(0, 2)
            file_size = file.tell()
            file.seek(0)

            if file_size > max_file_size:
                return jsonify(
                    {"error": f"File too large, max {max_file_size // (1024 * 1024)}MB"}
                ), 400

            session_id = request.form.get("session_id", "")
            save_to_workspace = False
            file_data = file.read()

            if session_id and workspace_available:
                session_type = "web"
                session = session_store.get_session(session_id)
                if session:
                    session_type = session.get("type", "web")
                else:
                    try:
                        sessions_file = os.path.join(server.data_dir, "sessions.json")
                        if os.path.exists(sessions_file):
                            with open(sessions_file, "r", encoding="utf-8") as f:
                                disk_sessions = json.load(f)
                            if session_id in disk_sessions:
                                session_type = disk_sessions[session_id].get(
                                    "type", "web"
                                )
                    except Exception as e:
                        server.log_message("warning", f"Load session metadata failed: {e}")

                try:
                    ws_result = workspace_manager.save_uploaded_file(
                        session_id, file_data, file.filename, session_type
                    )
                    if ws_result.get("success"):
                        save_to_workspace = True
                        server.log_message(
                            "info", f"Uploaded file saved to workspace: {file.filename}"
                        )
                except Exception as e:
                    server.log_message("error", f"Save file to workspace failed: {e}")

            if save_to_workspace:
                content = None
                if ws_result.get("mime_type", "").startswith("text/") or any(
                    file.filename.lower().endswith(ext)
                    for ext in [".txt", ".md", ".json", ".xml", ".csv"]
                ):
                    try:
                        ws_file_path = ws_result.get("path", "")
                        if ws_file_path and os.path.exists(ws_file_path):
                            with open(
                                ws_file_path, "r", encoding="utf-8", errors="ignore"
                            ) as f:
                                content = f.read()
                                if (
                                    len(content.encode("utf-8"))
                                    > preview_text_size_limit
                                ):
                                    content = content[:preview_text_size_limit]
                    except Exception as e:
                        server.log_message(
                            "warning", f"Read workspace text preview failed: {e}"
                        )

                return jsonify(
                    {
                        "success": True,
                        "filename": ws_result.get("filename", file.filename),
                        "path": ws_result.get("path", ""),
                        "url": f"/api/sessions/{session_id}/workspace/files/{ws_result.get('filename', file.filename)}",
                        "size": ws_result.get("size", file_size),
                        "content": content,
                        "in_workspace": True,
                    }
                )

            import hashlib

            file.seek(0)
            file_ext = os.path.splitext(file.filename)[1]
            unique_name = (
                hashlib.md5(f"{file.filename}{time.time()}".encode()).hexdigest()[:16]
                + file_ext
            )

            upload_dir = os.path.join(server.static_folder, "uploads")
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, unique_name)
            file.save(file_path)

            content = None
            try:
                if file_ext.lower() in [".txt", ".md", ".json", ".xml", ".csv"]:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if len(content.encode("utf-8")) > preview_text_size_limit:
                            content = content[:preview_text_size_limit]
                elif file_ext.lower() in [".docx"]:
                    try:
                        import docx

                        doc = docx.Document(file_path)
                        content = "\n".join([para.text for para in doc.paragraphs])
                        if len(content.encode("utf-8")) > preview_text_size_limit:
                            content = content[:preview_text_size_limit]
                    except ImportError:
                        content = None
            except Exception as e:
                server.log_message("warning", f"Read upload text preview failed: {e}")

            return jsonify(
                {
                    "success": True,
                    "filename": file.filename,
                    "unique_name": unique_name,
                    "path": f"/static/uploads/{unique_name}",
                    "url": f"/static/uploads/{unique_name}",
                    "size": os.path.getsize(file_path),
                    "content": content,
                    "in_workspace": False,
                }
            )
        except Exception as e:
            server.log_message("error", f"Upload failed: {e}")
            return jsonify({"error": str(e)}), 500
