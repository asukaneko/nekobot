from datetime import datetime

from flask import jsonify, request


def register_memory_routes(app, server):
    @app.route("/api/memory")
    def get_memory():
        mem_type = request.args.get("type", "all")
        target_id = request.args.get("target_id", "")

        if getattr(server, "PROMPT_MANAGER_AVAILABLE", False) and getattr(
            server, "prompt_manager", None
        ):
            try:
                memories = server.prompt_manager.get_memories(
                    target_id, mem_type if mem_type != "all" else None
                )
            except Exception:
                memories = server.memories
        else:
            memories = server.memories

        if mem_type != "all":
            memories = [m for m in memories if m.get("type", "long") == mem_type]
        if target_id:
            memories = [m for m in memories if m.get("target_id", "") == target_id]

        long_term = [m for m in memories if m.get("type", "long") == "long"]
        short_term = [m for m in memories if m.get("type", "long") == "short"]
        return jsonify(
            {"memories": memories, "long_term": long_term, "short_term": short_term}
        )

    @app.route("/api/memory", methods=["POST"])
    def add_memory():
        data = request.json or {}
        target_id = data.get("target_id", "")
        title = data.get("title", "")
        content = data.get("content", "")
        summary = data.get("summary")
        mem_type = data.get("type", "long")
        expire_days = data.get("expire_days", 7)

        if not title or not content:
            return jsonify({"success": False, "error": "title and content are required"}), 400

        prompt_manager = getattr(server, "prompt_manager", None)
        if not prompt_manager:
            return jsonify({"success": False, "error": "Prompt manager not available"}), 503

        success = prompt_manager.add_memory(
            title, content, target_id, summary, mem_type, expire_days
        )
        if success:
            server.memories = prompt_manager.get_memories()
            server._save_data("memories")
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Failed to add memory"}), 500

    @app.route("/api/memory/<memory_id>", methods=["PUT"])
    def update_memory(memory_id):
        data = request.json or {}

        for mem in server.memories:
            if mem.get("id") == memory_id:
                if "title" in data:
                    mem["title"] = data.get("title", mem.get("title", ""))
                elif "key" in data:
                    mem["title"] = data.get("key", mem.get("title", ""))

                if "content" in data:
                    mem["content"] = data.get("content", mem.get("content", ""))
                elif "value" in data:
                    mem["content"] = data.get("value", mem.get("content", ""))

                mem["summary"] = data.get("summary", mem.get("summary", ""))
                mem["type"] = data.get("type", mem.get("type", "long"))
                mem["priority"] = data.get("priority", mem.get("priority", "normal"))
                mem["expire_days"] = data.get("expire_days", mem.get("expire_days", 7))
                mem["target_id"] = data.get("target_id", mem.get("target_id", ""))
                mem["updated_at"] = datetime.now().isoformat()
                server._save_data("memories")

                prompt_manager = getattr(server, "prompt_manager", None)
                if prompt_manager:
                    try:
                        pm_memories = prompt_manager.get_memories()
                        for pm_mem in pm_memories:
                            if pm_mem.get("id") == memory_id:
                                pm_mem.update(mem)
                                break
                    except Exception:
                        pass

                return jsonify({"success": True, "memory": mem})
        return jsonify({"error": "Memory not found"}), 404

    @app.route("/api/memory/<memory_id>", methods=["DELETE"])
    def delete_memory(memory_id):
        prompt_manager = getattr(server, "prompt_manager", None)
        if not prompt_manager:
            return jsonify({"success": False, "error": "Prompt manager not available"}), 503

        success = prompt_manager.delete_memory(memory_id)
        if success:
            server.memories = [m for m in server.memories if m.get("id") != memory_id]
            server._save_data("memories")
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Failed to delete memory"}), 500

    @app.route("/api/memory", methods=["DELETE"])
    def clear_all_memory():
        target_id = request.args.get("target_id")
        prompt_manager = getattr(server, "prompt_manager", None)
        if not prompt_manager:
            return jsonify({"success": False, "error": "Prompt manager not available"}), 503

        success = prompt_manager.clear_memories(target_id)
        if success:
            if target_id:
                server.memories = [
                    m for m in server.memories if m.get("target_id") != target_id
                ]
            else:
                server.memories = []
            server._save_data("memories")
            return jsonify({"success": True})
        return jsonify({"success": False, "error": "Failed to clear memories"}), 500

    @app.route("/api/memory/export")
    def export_memory():
        return jsonify({"memories": server.memories})
