import logging
import uuid

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_skill_routes(app, server):
    @app.route("/api/skills")
    def get_skills():
        return jsonify(server.skills_config)

    @app.route("/api/skills", methods=["POST"])
    def create_skill():
        data = request.json or {}
        skill_name = data.get("name", "")
        skill = {
            "id": str(uuid.uuid4()),
            "name": skill_name,
            "description": data.get("description", ""),
            "aliases": data.get("aliases", []),
            "enabled": data.get("enabled", True),
            "parameters": data.get("parameters", {}),
        }
        server.skills_config.append(skill)
        server._save_data("skills")

        storage_created = False
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            if not manager.skill_exists(skill_name):
                storage = manager.create_skill(
                    skill_name,
                    {
                        "name": skill_name,
                        "description": data.get("description", ""),
                        "config": skill,
                    },
                )
                skill_md_content = data.get("skill_md", "")
                if skill_md_content:
                    storage.save_skill_md(skill_md_content)
                storage_created = True
        except Exception as e:
            _log.error(f"Failed to create skill storage: {e}")

        return jsonify(
            {"success": True, "skill": skill, "storage_created": storage_created}
        )

    @app.route("/api/skills/<skill_id>", methods=["PUT"])
    def update_skill(skill_id):
        data = request.json or {}
        for skill in server.skills_config:
            if skill["id"] == skill_id:
                old_name = skill["name"]
                new_name = data.get("name", skill["name"])

                skill["name"] = new_name
                skill["description"] = data.get("description", skill["description"])
                skill["aliases"] = data.get("aliases", skill["aliases"])
                skill["enabled"] = data.get("enabled", skill["enabled"])
                skill["parameters"] = data.get("parameters", skill["parameters"])
                server._save_data("skills")

                try:
                    from nbot.core.skills_manager import get_skills_storage_manager

                    manager = get_skills_storage_manager()
                    if old_name != new_name:
                        if manager.skill_exists(old_name):
                            new_storage = manager.create_skill(
                                new_name,
                                {
                                    "name": new_name,
                                    "description": data.get("description", ""),
                                    "config": skill,
                                },
                            )
                            old_storage = manager.get_skill_storage(old_name)
                            for script in old_storage.list_scripts():
                                content = old_storage.load_script(script)
                                if content:
                                    new_storage.save_script(script, content)
                            for resource in old_storage.list_resources():
                                content = old_storage.load_resource(resource)
                                if content:
                                    new_storage.save_resource(resource, content)
                            manager.delete_skill(old_name)
                    else:
                        storage = manager.get_skill_storage(new_name)
                        storage.save_config(
                            {
                                "name": new_name,
                                "description": data.get("description", ""),
                                "config": skill,
                            }
                        )
                except Exception as e:
                    _log.error(f"Failed to update skill storage: {e}")

                return jsonify({"success": True, "skill": skill})
        return jsonify({"error": "Skill not found"}), 404

    @app.route("/api/skills/<skill_id>", methods=["DELETE"])
    def delete_skill(skill_id):
        skill_to_delete = None
        for skill in server.skills_config:
            if skill["id"] == skill_id:
                skill_to_delete = skill
                break

        skill_name = skill_to_delete.get("name") if skill_to_delete else None
        server.skills_config = [s for s in server.skills_config if s["id"] != skill_id]
        server._save_data("skills")

        if skill_name:
            try:
                from nbot.core.skills_manager import get_skills_storage_manager

                manager = get_skills_storage_manager()
                if manager.skill_exists(skill_name):
                    manager.delete_skill(skill_name)
            except Exception as e:
                _log.error(f"Failed to delete skill storage: {e}")

        return jsonify({"success": True})

    @app.route("/api/skills/<skill_id>/toggle", methods=["POST"])
    def toggle_skill(skill_id):
        for skill in server.skills_config:
            if skill["id"] == skill_id:
                skill["enabled"] = not skill.get("enabled", True)
                server._save_data("skills")
                return jsonify({"success": True, "enabled": skill["enabled"]})
        return jsonify({"error": "Skill not found"}), 404
