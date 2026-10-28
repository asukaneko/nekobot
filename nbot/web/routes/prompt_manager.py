"""Prompt Manager（提示词预设）路由。

前端「提示词管理」页的后端：preset 列表 / 编辑 / 排序（=段落顺序）/ 启用开关，
酒馆 preset JSON 导入导出，以及会话级绑定。

路由::

    GET    /api/prompt-presets                        列表（含默认 id、上限、计数）
    POST   /api/prompt-presets                        新建
    GET    /api/prompt-presets/<preset_id>            详情
    PUT    /api/prompt-presets/<preset_id>            更新（全量）
    DELETE /api/prompt-presets/<preset_id>            删除
    POST   /api/prompt-presets/<preset_id>/default    设为全局默认
    POST   /api/prompt-presets/<preset_id>/reorder    段落重排 {"order": [section_id, ...]}
    GET    /api/prompt-presets/<preset_id>/preview    组装预览（可选 ?base_prompt=）
    GET    /api/prompt-presets/<preset_id>/export     导出（?format=native|tavern）
    POST   /api/prompt-presets/import                 导入（酒馆或原生 JSON）
    GET    /api/prompt-presets/session-binding        查询会话绑定（?session_id=）
    PUT    /api/prompt-presets/session-binding        设置/清除会话绑定
    GET    /api/prompt-presets/session-bindings       列出全部绑定（会话 → preset）

存储：``data/web/prompt_presets.json``（见 ``nbot/core/prompt_manager.py``）。
"""

import logging
from typing import Any

from flask import jsonify, request

from nbot.core.macros import MacroContext, expand
from nbot.core.prompt_manager import (
    MAX_PRESETS,
    MAX_SECTIONS,
    VALID_POSITIONS,
    VALID_ROLES,
    build_preset_injection,
    describe_preset,
    get_preset_store,
    insert_preset_messages,
    normalize_preset,
    normalize_sections,
)

_log = logging.getLogger(__name__)

_SCHEMA = {
    "roles": list(VALID_ROLES),
    "positions": list(VALID_POSITIONS),
    "max_presets": MAX_PRESETS,
    "max_sections": MAX_SECTIONS,
}


def _payload(store) -> dict[str, Any]:
    presets: list[dict[str, Any]] = store.list()
    default_id = store.default_id()
    return {
        "success": True,
        "presets": presets,
        "summaries": [describe_preset(preset) for preset in presets],
        "default_id": default_id,
        "count": len(presets),
        "limit": MAX_PRESETS,
        "schema": _SCHEMA,
    }


def _with_preset(store, preset: dict[str, Any]) -> dict[str, Any]:
    payload = _payload(store)
    payload["preset"] = preset
    payload["summary"] = describe_preset(preset)
    return payload


def _client_messages(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    messages: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user").strip().lower()
        if role not in VALID_ROLES:
            role = "user"
        content = str(item.get("content") or "")
        messages.append({"role": role, "content": content})
    return messages


def register_prompt_preset_routes(app, server):
    store_getter = getattr(server, "prompt_preset_store", None)
    if not callable(store_getter):
        store_getter = get_preset_store

    @app.route("/api/prompt-presets", methods=["GET"])
    def list_prompt_presets():
        store = store_getter()
        try:
            store.ensure_seed()
        except Exception as exc:  # pragma: no cover - 种子写入失败不阻断列表
            _log.debug("[PromptManager] 写入默认 preset 失败: %s", exc)
        return jsonify(_payload(store))

    @app.route("/api/prompt-presets", methods=["POST"])
    def create_prompt_preset():
        data = request.json or {}
        store = store_getter()
        try:
            preset = store.save(data)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify(_with_preset(store, preset))

    @app.route("/api/prompt-presets/<preset_id>", methods=["GET"])
    def get_prompt_preset(preset_id):
        store = store_getter()
        preset = store.get(preset_id)
        if preset is None:
            return jsonify({"success": False, "error": "preset 不存在"}), 404
        return jsonify(_with_preset(store, preset))

    @app.route("/api/prompt-presets/<preset_id>", methods=["PUT"])
    def update_prompt_preset(preset_id):
        store = store_getter()
        if store.get(preset_id) is None:
            return jsonify({"success": False, "error": "preset 不存在"}), 404
        data = dict(request.json or {})
        data["id"] = preset_id
        try:
            preset = store.save(data)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify(_with_preset(store, preset))

    @app.route("/api/prompt-presets/<preset_id>", methods=["DELETE"])
    def delete_prompt_preset(preset_id):
        store = store_getter()
        if not store.delete(preset_id):
            return jsonify({"success": False, "error": "preset 不存在"}), 404
        return jsonify(_payload(store))

    @app.route("/api/prompt-presets/<preset_id>/default", methods=["POST"])
    def set_default_prompt_preset(preset_id):
        store = store_getter()
        if not store.set_default(preset_id):
            return jsonify({"success": False, "error": "preset 不存在"}), 404
        return jsonify(_payload(store))

    @app.route("/api/prompt-presets/<preset_id>/reorder", methods=["POST"])
    def reorder_prompt_preset(preset_id):
        data = request.json or {}
        store = store_getter()
        preset = store.reorder(preset_id, data.get("order"))
        if preset is None:
            return jsonify({"success": False, "error": "preset 不存在或 order 非法"}), 404
        return jsonify(_with_preset(store, preset))

    @app.route("/api/prompt-presets/<preset_id>/preview", methods=["GET", "POST"])
    def preview_prompt_preset(preset_id):
        store = store_getter()
        preset = store.get(preset_id)
        if preset is None:
            return jsonify({"success": False, "error": "preset 不存在"}), 404

        data = request.get_json(silent=True) or {}
        base_prompt = data.get("base_prompt")
        if base_prompt is None:
            base_prompt = request.args.get("base_prompt", "")
        base_prompt = str(base_prompt or "")
        history = _client_messages(data.get("messages")) or [
            {"role": "user", "content": "（示例）你好呀"},
        ]

        variables = data.get("variables")
        macro_context = MacroContext(
            user_name=str(data.get("user_name") or "用户"),
            char_name=str(data.get("char_name") or "角色"),
            variables=dict(variables) if isinstance(variables, dict) else {},
            seed="prompt-preview",
        )
        injection = build_preset_injection(preset, expander=lambda text: expand(text, macro_context))
        system_parts = [part for part in (injection["start"], base_prompt, injection["end"]) if part]
        system_prompt = "\n\n".join(system_parts)

        assembled: list[dict[str, str]] = []
        if system_prompt:
            assembled.append({"role": "system", "content": system_prompt})
        assembled.extend(history)
        assembled = insert_preset_messages(injection, assembled)
        ordered_depth = sorted(
            injection["depth"],
            key=lambda item: (-int(item["depth"]), int(item["order"])),
        )

        return jsonify({
            "success": True,
            "preset": preset,
            "summary": describe_preset(preset),
            "system_start": injection["start"],
            "system_end": injection["end"],
            "base_prompt": base_prompt,
            "system_prompt": system_prompt,
            "top_messages": injection["top_messages"],
            "depth_insertions": [
                {"depth": item["depth"], "role": item["role"], "content": item["content"]}
                for item in ordered_depth
            ],
            "messages": assembled,
            "chars": injection["chars"],
        })

    @app.route("/api/prompt-presets/<preset_id>/export", methods=["GET"])
    def export_prompt_preset(preset_id):
        store = store_getter()
        fmt = request.args.get("format", "native")
        data = store.export_preset(preset_id, fmt=fmt)
        if data is None:
            return jsonify({"success": False, "error": "preset 不存在"}), 404
        return jsonify({"success": True, "format": fmt, "preset": data})

    @app.route("/api/prompt-presets/import", methods=["POST"])
    def import_prompt_preset():
        payload = request.get_json(silent=True)
        if payload is None:
            return jsonify({"success": False, "error": "缺少导入内容"}), 400
        mode = "merge"
        if isinstance(payload, dict) and "mode" in payload:
            mode = payload.get("mode", "merge")
        store = store_getter()
        try:
            result = store.import_preset(payload, mode=mode)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        response = _payload(store)
        response.update({
            "added": result["added"],
            "updated": result["updated"],
            "skipped": result["skipped"],
            "warnings": result["warnings"],
            "imported": result["preset"],
        })
        return jsonify(response)

    @app.route("/api/prompt-presets/session-bindings", methods=["GET"])
    def list_session_prompt_bindings():
        """列出所有会话 → preset 绑定（悬空绑定已过滤）。"""
        store = store_getter()
        names = {preset["id"]: preset["name"] for preset in store.list()}
        bindings = {
            session_id: preset_id
            for session_id, preset_id in store.bindings().items()
            if preset_id in names
        }
        return jsonify({
            "success": True,
            "bindings": bindings,
            "preset_names": names,
            "count": len(bindings),
        })

    @app.route("/api/prompt-presets/session-binding", methods=["GET"])
    def get_session_prompt_binding():
        session_id = str(request.args.get("session_id") or "").strip()
        store = store_getter()
        preset_id = store.bound_preset_id(session_id)
        preset = store.get(preset_id) if preset_id else None
        return jsonify({
            "success": True,
            "session_id": session_id,
            "preset_id": preset_id if preset is not None else "",
            "preset": preset,
            "default_id": store.default_id(),
        })

    @app.route("/api/prompt-presets/session-binding", methods=["PUT"])
    def set_session_prompt_binding():
        data = request.json or {}
        session_id = str(data.get("session_id") or "").strip()
        if not session_id:
            return jsonify({"success": False, "error": "session_id 不能为空"}), 400
        preset_id = str(data.get("preset_id") or "").strip()
        store = store_getter()
        if not store.bind_session(session_id, preset_id):
            return jsonify({"success": False, "error": "preset 不存在"}), 400
        preset = store.get(preset_id) if preset_id else None
        return jsonify({
            "success": True,
            "session_id": session_id,
            "preset_id": preset_id,
            "preset": preset,
            "summary": describe_preset(preset) if preset else None,
        })


def preset_section_defaults() -> dict[str, Any]:
    """给前端/文档用的段落字段默认值。"""
    sample = normalize_preset({"name": "sample", "sections": [{"content": "x"}]})
    sections = normalize_sections((sample or {}).get("sections"))
    return sections[0] if sections else {}
