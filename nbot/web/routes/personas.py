"""用户人设（persona）库路由。

维护"对话里的我"这一侧设定：名称/代词/背景。会话通过 ``persona_id`` 绑定，
未绑定回退全局默认；命中后由 ``nbot/core/personas.py`` 生成 system prompt 注入块。

路由::

    GET    /api/personas                 列表 + 默认 id
    POST   /api/personas                 新建
    PUT    /api/personas/<persona_id>    更新
    DELETE /api/personas/<persona_id>    删除
    POST   /api/personas/<persona_id>/default   设为全局默认
    GET    /api/personas/<persona_id>/preview   prompt 预览
    GET    /api/personas/export          导出 JSON
    POST   /api/personas/import          导入（merge / overwrite）

会话绑定走既有的 ``PUT /api/sessions/<id>``（字段 ``persona_id``）。
"""

import logging
from typing import Any, Dict

from flask import jsonify, request

from nbot.core.personas import (
    MAX_PERSONAS,
    build_persona_block,
    get_persona_store,
)

_log = logging.getLogger(__name__)


def _persona_payload(store) -> Dict[str, Any]:
    personas = store.list()
    default_id = store.default_id()
    return {
        "success": True,
        "personas": personas,
        "default_id": default_id,
        "limit": MAX_PERSONAS,
        "count": len(personas),
    }


def register_persona_routes(app, server):
    @app.route("/api/personas", methods=["GET"])
    def get_personas():
        return jsonify(_persona_payload(get_persona_store()))

    @app.route("/api/personas", methods=["POST"])
    def create_persona():
        data = request.json or {}
        store = get_persona_store()
        try:
            persona = store.save(data)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        payload = _persona_payload(store)
        payload["persona"] = persona
        return jsonify(payload)

    @app.route("/api/personas/<persona_id>", methods=["PUT"])
    def update_persona(persona_id):
        store = get_persona_store()
        if store.get(persona_id) is None:
            return jsonify({"success": False, "error": "人设不存在"}), 404
        data = dict(request.json or {})
        data["id"] = persona_id
        try:
            persona = store.save(data)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        payload = _persona_payload(store)
        payload["persona"] = persona
        return jsonify(payload)

    @app.route("/api/personas/<persona_id>", methods=["DELETE"])
    def delete_persona(persona_id):
        store = get_persona_store()
        if not store.delete(persona_id):
            return jsonify({"success": False, "error": "人设不存在"}), 404
        return jsonify(_persona_payload(store))

    @app.route("/api/personas/<persona_id>/default", methods=["POST"])
    def set_default_persona(persona_id):
        store = get_persona_store()
        if not store.set_default(persona_id):
            return jsonify({"success": False, "error": "人设不存在"}), 404
        return jsonify(_persona_payload(store))

    @app.route("/api/personas/<persona_id>/preview", methods=["GET"])
    def preview_persona(persona_id):
        store = get_persona_store()
        persona = store.get(persona_id)
        if persona is None:
            return jsonify({"success": False, "error": "人设不存在"}), 404
        block = build_persona_block(persona)
        return jsonify({"success": True, "persona": persona, "prompt_block": block})

    @app.route("/api/personas/export", methods=["GET"])
    def export_personas():
        store = get_persona_store()
        return jsonify({
            "success": True,
            "version": 1,
            "personas": store.list(),
            "default_id": store.default_id(),
        })

    @app.route("/api/personas/import", methods=["POST"])
    def import_personas():
        data = request.json or {}
        items = data.get("personas")
        if items is None and isinstance(data, list):
            items = data
        store = get_persona_store()
        try:
            result = store.import_personas(items, mode=data.get("mode", "merge"))
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        payload = _persona_payload(store)
        payload.update(result)
        return jsonify(payload)
