import uuid
from datetime import datetime

from flask import jsonify, request


def register_workflow_routes(app, server):
    @app.route("/api/workflows")
    def get_workflows():
        return jsonify(server.workflows)

    @app.route("/api/workflows", methods=["POST"])
    def create_workflow():
        data = request.json or {}
        workflow = {
            "id": str(uuid.uuid4()),
            "name": data.get("name", "新工作流"),
            "description": data.get("description", ""),
            "enabled": data.get("enabled", True),
            "trigger": data.get("trigger", "manual"),
            "config": data.get("config", {}),
        }
        server.workflows.append(workflow)
        server._save_data("workflows")

        if workflow.get("enabled") and workflow.get("trigger") == "cron":
            server._schedule_workflow(workflow)

        return jsonify({"success": True, "workflow": workflow})

    @app.route("/api/workflows/<workflow_id>", methods=["PUT"])
    def update_workflow(workflow_id):
        for workflow in server.workflows:
            if workflow["id"] == workflow_id:
                data = request.json or {}
                old_trigger = workflow.get("trigger")

                workflow.update(data)
                server._save_data("workflows")

                if workflow.get("trigger") == "cron":
                    if workflow.get("enabled"):
                        server._schedule_workflow(workflow)
                    else:
                        server._unschedule_workflow(workflow_id)
                elif old_trigger == "cron":
                    server._unschedule_workflow(workflow_id)

                return jsonify({"success": True, "workflow": workflow})
        return jsonify({"error": "Workflow not found"}), 404

    @app.route("/api/workflows/<workflow_id>", methods=["DELETE"])
    def delete_workflow(workflow_id):
        server._unschedule_workflow(workflow_id)
        server.workflows = [w for w in server.workflows if w["id"] != workflow_id]
        server._save_data("workflows")
        return jsonify({"success": True})

    @app.route("/api/workflows/<workflow_id>/toggle", methods=["POST"])
    def toggle_workflow(workflow_id):
        for workflow in server.workflows:
            if workflow["id"] == workflow_id:
                workflow["enabled"] = not workflow.get("enabled", True)
                server._save_data("workflows")

                if workflow.get("trigger") == "cron":
                    if workflow["enabled"]:
                        server._schedule_workflow(workflow)
                    else:
                        server._unschedule_workflow(workflow_id)

                return jsonify({"success": True, "enabled": workflow["enabled"]})
        return jsonify({"error": "Workflow not found"}), 404

    @app.route("/api/workflows/<workflow_id>/execute", methods=["POST"])
    def execute_workflow(workflow_id):
        for workflow in server.workflows:
            if workflow["id"] == workflow_id:
                data = request.json or {}
                trigger_data = {
                    "source": "manual",
                    "content": data.get("content", ""),
                    "time": datetime.now().isoformat(),
                }
                server._execute_workflow(workflow_id, trigger_data)
                return jsonify({"success": True, "message": "Workflow execution started"})
        return jsonify({"error": "Workflow not found"}), 404
