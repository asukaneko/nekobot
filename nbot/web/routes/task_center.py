import uuid
from copy import deepcopy
from datetime import datetime

from flask import jsonify, request


def _normalize_task_payload(data):
    config = data.get("config") or {}
    return {
        "name": (data.get("name") or "新任务").strip() or "新任务",
        "description": (data.get("description") or "").strip(),
        "enabled": bool(data.get("enabled", True)),
        "trigger": data.get("trigger", "interval"),
        "config": {
            "interval_minutes": int(config.get("interval_minutes", 60) or 60),
            "cron": (config.get("cron") or "0 8 * * *").strip() or "0 8 * * *",
            "run_at": config.get("run_at") or "",
        },
        "target_session_id": (data.get("target_session_id") or "").strip(),
        "prompt": (data.get("prompt") or "").strip(),
    }


def register_task_center_routes(app, server):
    @app.route("/api/task-center")
    def get_task_center():
        return jsonify({"items": server.get_task_center_items()})

    @app.route("/api/task-center", methods=["POST"])
    def create_task_center_task():
        payload = _normalize_task_payload(request.json or {})
        task = {
            "id": str(uuid.uuid4()),
            "kind": "custom",
            "created_at": datetime.now().isoformat(),
            "last_run": None,
            "next_run": None,
            **payload,
        }
        server.scheduled_tasks.append(task)

        if task.get("enabled"):
            server._schedule_custom_task(task)

        server._save_data("scheduled_tasks")
        return jsonify({"success": True, "task": task})

    @app.route("/api/task-center/<task_id>", methods=["PUT"])
    def update_task_center_task(task_id):
        task = server._get_custom_task(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404

        payload = _normalize_task_payload(request.json or {})
        task.update(payload)
        server._unschedule_custom_task(task_id)
        if task.get("enabled"):
            server._schedule_custom_task(task)

        server._save_data("scheduled_tasks")
        return jsonify({"success": True, "task": task})

    @app.route("/api/task-center/<task_id>", methods=["DELETE"])
    def delete_task_center_task(task_id):
        task = server._get_custom_task(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404

        server._unschedule_custom_task(task_id)
        server.scheduled_tasks = [item for item in server.scheduled_tasks if item.get("id") != task_id]
        server._save_data("scheduled_tasks")
        return jsonify({"success": True})

    @app.route("/api/task-center/<task_id>/toggle", methods=["POST"])
    def toggle_task_center_item(task_id):
        if task_id == "heartbeat":
            server.heartbeat_config["enabled"] = not server.heartbeat_config.get("enabled", False)
            if server.heartbeat_config["enabled"]:
                server._start_heartbeat_job(server.heartbeat_config.get("interval_minutes", 60))
            else:
                server._stop_heartbeat_job()
            server._save_data("heartbeat")
            return jsonify({"success": True, "item": deepcopy(server.heartbeat_config)})

        workflow = next((item for item in server.workflows if item.get("id") == task_id), None)
        if workflow:
            workflow["enabled"] = not workflow.get("enabled", True)
            if workflow.get("trigger") == "cron":
                if workflow["enabled"]:
                    server._schedule_workflow(workflow)
                else:
                    server._unschedule_workflow(task_id)
            server._save_data("workflows")
            return jsonify({"success": True, "item": workflow})

        task = server._get_custom_task(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404

        task["enabled"] = not task.get("enabled", True)
        if task["enabled"]:
            server._schedule_custom_task(task)
        else:
            server._unschedule_custom_task(task_id)
        server._save_data("scheduled_tasks")
        return jsonify({"success": True, "item": task})

    @app.route("/api/task-center/<task_id>/run", methods=["POST"])
    def run_task_center_item(task_id):
        if task_id == "heartbeat":
            import asyncio

            asyncio.run(server._execute_heartbeat(force=True))
            return jsonify({"success": True})

        workflow = next((item for item in server.workflows if item.get("id") == task_id), None)
        if workflow:
            trigger_data = {
                "source": "task-center",
                "content": (request.json or {}).get("content", ""),
                "time": datetime.now().isoformat(),
            }
            server._execute_workflow(task_id, trigger_data)
            return jsonify({"success": True})

        task = server._get_custom_task(task_id)
        if not task:
            return jsonify({"error": "Task not found"}), 404

        server._execute_custom_task(task_id)
        return jsonify({"success": True})
