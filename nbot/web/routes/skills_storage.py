import json
import logging
import os
import uuid

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_skills_storage_routes(app, server):
    @app.route("/api/skills/storage", methods=["GET"])
    def get_skills_storage():
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            return jsonify(manager.list_skills())
        except Exception as e:
            _log.error(f"Failed to list skill storage: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>", methods=["GET"])
    def get_skill_storage(skill_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            info = manager.get_skill_info(skill_name)
            if not info:
                return jsonify({"error": "Skill not found"}), 404
            return jsonify(
                {
                    "name": skill_name,
                    "skill_md": info.get("skill_md"),
                    "reference_md": info.get("reference_md"),
                    "license": info.get("license"),
                    "files": info.get("files", []),
                    "scripts": info.get("scripts", []),
                    "resources": info.get("resources", []),
                }
            )
        except Exception as e:
            _log.error(f"Failed to get skill storage {skill_name}: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>/skill-md", methods=["GET", "POST"])
    def skill_md_file(skill_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            storage = get_skills_storage_manager().get_skill_storage(skill_name)
            if request.method == "GET":
                return jsonify({"content": storage.load_skill_md() or ""})

            storage.save_skill_md((request.json or {}).get("content", ""))
            return jsonify({"success": True, "message": "SKILL.md saved"})
        except Exception as e:
            _log.error(f"Failed to access SKILL.md for {skill_name}: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route(
        "/api/skills/storage/<skill_name>/reference-md", methods=["GET", "POST"]
    )
    def reference_md_file(skill_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            storage = get_skills_storage_manager().get_skill_storage(skill_name)
            if request.method == "GET":
                return jsonify({"content": storage.load_reference_md() or ""})

            storage.save_reference_md((request.json or {}).get("content", ""))
            return jsonify({"success": True, "message": "reference.md saved"})
        except Exception as e:
            _log.error(
                f"Failed to access reference.md for {skill_name}: {e}",
                exc_info=True,
            )
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>/file/<path:file_name>", methods=["GET"])
    def get_skill_file(skill_name, file_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            storage = get_skills_storage_manager().get_skill_storage(skill_name)
            file_path = os.path.join(storage.skill_dir, file_name)
            if not os.path.exists(file_path):
                return jsonify({"error": f"File not found: {file_name}"}), 404

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    return jsonify({"content": f.read(), "file_name": file_name})
            except UnicodeDecodeError:
                return jsonify({"error": "Binary files are not supported"}), 400
        except Exception as e:
            _log.error(
                f"Failed to read skill file {skill_name}/{file_name}: {e}",
                exc_info=True,
            )
            return jsonify({"error": str(e)}), 500

    @app.route(
        "/api/skills/storage/<skill_name>/file/<path:file_name>", methods=["POST"]
    )
    def save_skill_file(skill_name, file_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            if not manager.skill_exists(skill_name):
                return jsonify({"error": "Skill not found"}), 404

            storage = manager.get_skill_storage(skill_name)
            file_path = os.path.join(storage.skill_dir, file_name)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write((request.json or {}).get("content", ""))

            return jsonify({"success": True, "message": "File saved"})
        except Exception as e:
            _log.error(
                f"Failed to save skill file {skill_name}/{file_name}: {e}",
                exc_info=True,
            )
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>/script/<script_name>", methods=["GET"])
    def get_skill_script(skill_name, script_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            storage = get_skills_storage_manager().get_skill_storage(skill_name)
            content = storage.load_script(script_name)
            if content is None:
                return jsonify({"error": "Script not found"}), 404
            return jsonify({"content": content})
        except Exception as e:
            _log.error(
                f"Failed to read skill script {skill_name}/{script_name}: {e}",
                exc_info=True,
            )
            return jsonify({"error": str(e)}), 500

    @app.route(
        "/api/skills/storage/<skill_name>/script/<script_name>", methods=["POST"]
    )
    def save_skill_script(skill_name, script_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            if not manager.skill_exists(skill_name):
                manager.create_skill(skill_name, {"name": skill_name})

            storage = manager.get_skill_storage(skill_name)
            storage.save_script(script_name, (request.json or {}).get("content", ""))
            return jsonify({"success": True, "message": "Script saved"})
        except Exception as e:
            _log.error(
                f"Failed to save skill script {skill_name}/{script_name}: {e}",
                exc_info=True,
            )
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>", methods=["DELETE"])
    def delete_skill_storage(skill_name):
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            if not manager.skill_exists(skill_name):
                return jsonify({"error": "Skill storage not found"}), 404

            if manager.delete_skill(skill_name):
                _log.info(f"Deleted skill storage: {skill_name}")
                return jsonify({"success": True, "message": "Skill storage deleted"})
            return jsonify({"error": "Delete failed"}), 500
        except Exception as e:
            _log.error(f"Failed to delete skill storage {skill_name}: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/upload-folder", methods=["POST"])
    def upload_skill_folder():
        try:
            from nbot.core.skills_manager import SKILLS_ROOT, get_skills_storage_manager

            folder_name = request.form.get("folder_name", "")
            skill_md = request.form.get("skill_md", "")
            skill_config_str = request.form.get("skill_config", "{}")
            if not folder_name:
                return jsonify({"error": "Folder name is required"}), 400

            try:
                skill_config = json.loads(skill_config_str)
            except json.JSONDecodeError:
                skill_config = {}

            manager = get_skills_storage_manager()
            if manager.skill_exists(folder_name):
                return jsonify({"error": f'Skill "{folder_name}" already exists'}), 409
            if not manager.create_skill(folder_name):
                return jsonify({"error": "Failed to create skill storage"}), 500

            saved_files = []
            for file in request.files.getlist("files"):
                if not file.filename:
                    continue
                file_path = file.filename.replace("..", "").lstrip("/\\")
                full_path = os.path.join(SKILLS_ROOT, folder_name, file_path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                file.save(full_path)
                saved_files.append(file_path)

            skill = {
                "id": str(uuid.uuid4()),
                "name": folder_name,
                "description": skill_config.get(
                    "description", f"Auto-imported skill {folder_name}"
                ),
                "aliases": skill_config.get("aliases", []),
                "enabled": True,
                "parameters": skill_config.get("parameters", {}),
                "has_storage": True,
                "scripts": [f for f in saved_files if f.endswith(".py")],
                "skill_md": skill_md,
            }
            server.skills_config.append(skill)
            server._save_data("skills")

            return jsonify(
                {
                    "success": True,
                    "skill": skill,
                    "message": f'Skill "{skill["name"]}" uploaded',
                    "files_count": len(saved_files),
                }
            )
        except Exception as e:
            _log.error(f"Failed to upload skill folder: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500
