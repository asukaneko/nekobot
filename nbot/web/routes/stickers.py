"""自定义表情包管理 API"""

import logging
import os

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_sticker_routes(app, server):
    from nbot.services import sticker_library

    @app.route("/api/stickers", methods=["GET"])
    def list_stickers_route():
        try:
            stickers = sticker_library.list_stickers()
            return jsonify({"success": True, "stickers": stickers, "total": len(stickers)})
        except Exception as e:
            _log.error("[Stickers] list failed: %s", e)
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/stickers", methods=["POST"])
    def upload_sticker_route():
        """上传单个表情（multipart: file + name）或 ZIP 批量导入。"""
        try:
            upload = request.files.get("file")
            if upload is None:
                return jsonify({"success": False, "error": "file is required"}), 400

            filename = upload.filename or "sticker.png"
            data = upload.read()
            lower = filename.lower()
            if lower.endswith(".zip"):
                result = sticker_library.import_stickers_zip(data)
                return jsonify({"success": True, **result})

            name = sticker_library.sanitize_sticker_name(
                request.form.get("name") or os.path.splitext(filename)[0]
            )
            if not name:
                return jsonify({"success": False, "error": "表情名称无效"}), 400
            sticker = sticker_library.add_sticker(
                name, data, ext=os.path.splitext(filename)[1]
            )
            return jsonify({"success": True, "sticker": sticker})
        except Exception as e:
            _log.error("[Stickers] upload failed: %s", e)
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/stickers/<path:name>", methods=["PUT"])
    def rename_sticker_route(name):
        try:
            data = request.get_json(silent=True) or {}
            new_name = sticker_library.sanitize_sticker_name(data.get("new_name") or "")
            if not new_name:
                return jsonify({"success": False, "error": "new_name is required"}), 400
            if sticker_library.rename_sticker(name, new_name):
                return jsonify({"success": True, "name": new_name})
            return jsonify({"success": False, "error": "改名失败（不存在或重名）"}), 404
        except Exception as e:
            _log.error("[Stickers] rename failed: %s", e)
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/stickers/<path:name>", methods=["DELETE"])
    def delete_sticker_route(name):
        try:
            if sticker_library.delete_sticker(name):
                return jsonify({"success": True})
            return jsonify({"success": False, "error": "表情不存在"}), 404
        except Exception as e:
            _log.error("[Stickers] delete failed: %s", e)
            return jsonify({"success": False, "error": str(e)}), 500
