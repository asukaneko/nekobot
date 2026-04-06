"""
API Key 管理器路由
用于管理保存的 API Keys
"""
import os
import json
import time
from flask import request, jsonify


def get_api_keys_file_path(server):
    """获取API Keys存储文件路径"""
    return os.path.join(server.data_dir, "api_keys.json")


def load_api_keys(server):
    """加载保存的API Keys"""
    file_path = get_api_keys_file_path(server)
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def save_api_keys(server, api_keys):
    """保存API Keys"""
    file_path = get_api_keys_file_path(server)
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(api_keys, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        return False


def register_api_key_routes(app, server):
    """注册API Key管理器路由"""

    @app.route("/api/api-keys", methods=["GET"])
    def get_api_keys():
        """获取所有保存的API Keys（不包含实际的key值，只返回名称和ID）"""
        try:
            api_keys = load_api_keys(server)
            # 只返回id和name，不返回实际的key值
            safe_keys = [
                {"id": key.get("id"), "name": key.get("name", "未命名")}
                for key in api_keys
            ]
            return jsonify({"success": True, "keys": safe_keys})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/api-keys/<key_id>", methods=["GET"])
    def get_api_key(key_id):
        """获取单个API Key的详细信息（包含实际的key值）"""
        try:
            api_keys = load_api_keys(server)
            key = next((k for k in api_keys if k.get("id") == key_id), None)
            if key:
                return jsonify({"success": True, "key": key})
            return jsonify({"error": "API Key not found"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/api-keys", methods=["POST"])
    def create_api_key():
        """创建新的API Key"""
        try:
            data = request.json
            name = data.get("name", "").strip()
            key_value = data.get("key", "").strip()

            if not name:
                return jsonify({"error": "Name is required"}), 400
            if not key_value:
                return jsonify({"error": "API Key is required"}), 400

            api_keys = load_api_keys(server)

            # 检查名称是否已存在
            if any(k.get("name") == name for k in api_keys):
                return jsonify({"error": "API Key name already exists"}), 400

            # 创建新的API Key
            new_key = {
                "id": f"key_{int(time.time() * 1000)}",
                "name": name,
                "key": key_value,
                "created_at": time.time()
            }

            api_keys.append(new_key)

            if save_api_keys(server, api_keys):
                return jsonify({
                    "success": True,
                    "key": {"id": new_key["id"], "name": new_key["name"]}
                })
            return jsonify({"error": "Failed to save API Key"}), 500

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/api-keys/<key_id>", methods=["PUT"])
    def update_api_key(key_id):
        """更新API Key"""
        try:
            data = request.json
            name = data.get("name", "").strip()
            key_value = data.get("key", "").strip()

            api_keys = load_api_keys(server)
            key_index = next((i for i, k in enumerate(api_keys) if k.get("id") == key_id), None)

            if key_index is None:
                return jsonify({"error": "API Key not found"}), 404

            # 检查名称是否与其他key冲突
            if name and any(k.get("name") == name and k.get("id") != key_id for k in api_keys):
                return jsonify({"error": "API Key name already exists"}), 400

            # 更新key
            if name:
                api_keys[key_index]["name"] = name
            if key_value:
                api_keys[key_index]["key"] = key_value
            api_keys[key_index]["updated_at"] = time.time()

            if save_api_keys(server, api_keys):
                return jsonify({
                    "success": True,
                    "key": {"id": key_id, "name": api_keys[key_index]["name"]}
                })
            return jsonify({"error": "Failed to save API Key"}), 500

        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/api-keys/<key_id>", methods=["DELETE"])
    def delete_api_key(key_id):
        """删除API Key"""
        try:
            api_keys = load_api_keys(server)
            key_index = next((i for i, k in enumerate(api_keys) if k.get("id") == key_id), None)

            if key_index is None:
                return jsonify({"error": "API Key not found"}), 404

            api_keys.pop(key_index)

            if save_api_keys(server, api_keys):
                return jsonify({"success": True})
            return jsonify({"error": "Failed to save API Keys"}), 500

        except Exception as e:
            return jsonify({"error": str(e)}), 500
