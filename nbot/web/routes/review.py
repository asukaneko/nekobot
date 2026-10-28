"""
Review Pipeline Web API

提供 Review 结果查询、MemoryFS 路径浏览等接口。

路由：
    GET  /api/review/logs               — 查询 Review 事件历史
    GET  /api/review/memory-fs          — 查询 MemoryFS 逻辑文件列表
    GET  /api/review/memory-fs/<path>   — 读取指定路径的逻辑文件
    GET  /api/review/event-stream       — 查询标准化事件流
    POST /api/review/run                — 手动触发一次 Review（调试用）
"""

import logging

from flask import jsonify, request

_log = logging.getLogger(__name__)


def _get_event_bus(server):
    """获取 event_bus，优先从 server.hook_runtime 读取，fallback 到全局 HookManager。"""
    hook_runtime = getattr(server, "hook_runtime", None)
    if hook_runtime:
        bus = getattr(hook_runtime, "_event_bus", None)
        if bus:
            return bus
    # fallback: 从全局 HookManager 获取
    try:
        data_dir = getattr(server, "data_dir", "data/web")
        from nbot.hooks.manager import get_hook_manager
        hm = get_hook_manager(data_dir=data_dir)
        return getattr(hm, "_event_bus", None)
    except Exception:
        return None


def _short_id(value, *, length=8):
    value = str(value or "").strip()
    if not value:
        return ""
    return value[-length:] if len(value) > length else value


def _get_session(server, conversation_id):
    sessions = getattr(server, "sessions", {}) or {}
    if not isinstance(sessions, dict):
        return {}
    session = sessions.get(conversation_id)
    return session if isinstance(session, dict) else {}


def _session_display_name(session, conversation_id):
    for key in ("name", "title", "display_name"):
        value = str((session or {}).get(key) or "").strip()
        if value:
            return value
    return f"会话 {_short_id(conversation_id)}" if conversation_id else ""


def _character_display_name(session, event):
    for key in ("sender_name", "character_name", "character_id"):
        value = str((session or {}).get(key) or "").strip()
        if value:
            return value
    return str((event or {}).get("character_id") or "").strip()


def _enrich_event_for_display(server, event):
    enriched = dict(event or {})
    conversation_id = enriched.get("conversation_id", "")
    session = _get_session(server, conversation_id)
    conversation_name = _session_display_name(session, conversation_id)
    character_name = _character_display_name(session, enriched)

    enriched["conversation_name"] = conversation_name
    enriched["conversation_label"] = conversation_name or _short_id(conversation_id)
    enriched["conversation_short_id"] = _short_id(conversation_id)
    enriched["character_label"] = character_name
    enriched["source_label"] = str(enriched.get("source") or "").strip()
    return enriched


def register_review_routes(app, server):

    # ------------------------------------------------------------------
    # Review 事件历史（来自 Hook event bus 历史）
    # ------------------------------------------------------------------

    @app.route("/api/review/logs")
    def get_review_logs():
        """查询 review.started / review.finished 事件历史。"""
        limit = int(request.args.get("limit", 50))
        conversation_id = request.args.get("conversation_id", "")

        try:
            bus = _get_event_bus(server)
            if not bus:
                return jsonify({"logs": [], "total": 0})

            history = bus.get_history(limit=200)
            review_events = [
                e for e in history
                if e.get("type", "").startswith("review.")
                and (not conversation_id or e.get("conversation_id") == conversation_id)
            ]
            logs = [_enrich_event_for_display(server, e) for e in review_events[-limit:]]
            return jsonify({"logs": logs, "total": len(review_events)})
        except Exception as exc:
            _log.error("[ReviewRoutes] get_review_logs failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------
    # MemoryFS 路径浏览
    # ------------------------------------------------------------------

    @app.route("/api/review/memory-fs")
    def list_memory_fs():
        """列出指定角色的所有 MemoryFS 逻辑文件。"""
        character_id = request.args.get("character_id", "")
        try:
            from nbot.memory.fs import describe_memory_path, get_memory_fs
            mfs = get_memory_fs()
            if character_id:
                files = mfs.list_for_character(character_id)
            else:
                files = list(mfs._index.values())
            items = []
            for mf in files:
                item = mf.to_dict()
                item.update(describe_memory_path(mf.path))
                items.append(item)
            items.sort(key=lambda item: (
                item.get("category_order", 99),
                -float(item.get("importance") or 0),
                item.get("path", ""),
            ))
            return jsonify({
                "files": items,
                "total": len(files),
            })
        except Exception as exc:
            _log.error("[ReviewRoutes] list_memory_fs failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/review/memory-fs/read")
    def read_memory_fs():
        """读取指定逻辑路径的 MemoryFile。"""
        path = request.args.get("path", "")
        if not path:
            return jsonify({"error": "path is required"}), 400
        try:
            from nbot.memory.fs import get_memory_fs
            mfs = get_memory_fs()
            mf = mfs.read(path)
            if mf is None:
                return jsonify({"error": "not found"}), 404
            return jsonify(mf.to_dict())
        except Exception as exc:
            _log.error("[ReviewRoutes] read_memory_fs failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

    @app.route("/api/review/memory-fs/delete", methods=["DELETE", "POST"])
    def delete_memory_fs():
        """删除指定逻辑路径的 MemoryFS 文件。

        参数（query 或 body json 均支持）：
            path         — 必填，逻辑路径
            character_id — 可选，限定角色前缀，避免越权删除
        """
        payload = request.get_json(silent=True) or {}
        path = (
            request.args.get("path")
            or payload.get("path")
            or ""
        )
        character_id = (
            request.args.get("character_id")
            or payload.get("character_id")
            or ""
        )
        if not path:
            return jsonify({"error": "path is required"}), 400
        # 安全校验：必须落在 characters/<char_id>/ 之下
        if character_id:
            prefix = f"characters/{character_id}/"
            if not (path == prefix.rstrip("/") or path.startswith(prefix)):
                return jsonify({
                    "error": f"path '{path}' does not belong to character '{character_id}'",
                }), 403
        try:
            from nbot.memory.fs import get_memory_fs
            mfs = get_memory_fs()
            existed = mfs.read(path) is not None
            if not existed:
                return jsonify({"error": "not found", "path": path}), 404
            mfs.delete(path)
            _log.info(
                "[ReviewRoutes] memory-fs deleted: path=%s char=%s",
                path, character_id or "*",
            )
            return jsonify({"ok": True, "path": path})
        except Exception as exc:
            _log.error("[ReviewRoutes] delete_memory_fs failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------
    # Hook 事件流（按新标准事件名筛选）
    # ------------------------------------------------------------------

    @app.route("/api/review/event-stream")
    def get_event_stream():
        """查询 Hook 事件流，支持按新标准事件域筛选。

        参数：
            domain   — 事件域前缀，如 character / plot / group / review
            limit    — 最大返回数量（默认 100）
            conversation_id — 按会话过滤
        """
        domain = request.args.get("domain", "")
        limit = int(request.args.get("limit", 100))
        conversation_id = request.args.get("conversation_id", "")

        try:
            bus = _get_event_bus(server)
            if not bus:
                return jsonify({"events": [], "total": 0})

            history = bus.get_history(limit=500)
            filtered = [
                e for e in history
                if (not domain or e.get("type", "").startswith(domain + "."))
                and (not conversation_id or e.get("conversation_id") == conversation_id)
            ]
            events = [_enrich_event_for_display(server, e) for e in filtered[-limit:]]
            return jsonify({"events": events, "total": len(filtered)})
        except Exception as exc:
            _log.error("[ReviewRoutes] get_event_stream failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------
    # 复盘模式设置
    # ------------------------------------------------------------------

    @app.route("/api/review/settings", methods=["GET"])
    def get_review_settings():
        from nbot.review.pipeline import get_review_mode
        mode = get_review_mode(getattr(server, "data_dir", "data/web"))
        return jsonify({"mode": mode})

    @app.route("/api/review/settings", methods=["POST"])
    def set_review_settings():
        data = request.json or {}
        mode = data.get("mode", "rule")
        if mode not in ("rule", "llm"):
            return jsonify({"error": "mode must be 'rule' or 'llm'"}), 400
        try:
            if not isinstance(server.settings, dict):
                server.settings = {}
            review_cfg = dict(server.settings.get("review") or {})
            review_cfg["mode"] = mode
            server.settings["review"] = review_cfg
            server._save_data("settings")
            from nbot.review.pipeline import get_review_pipeline
            get_review_pipeline().set_mode(mode)
            return jsonify({"ok": True, "mode": mode})
        except Exception as exc:
            _log.error("[ReviewRoutes] set review settings failed: %s", exc)
            return jsonify({"error": str(exc)}), 500

    # ------------------------------------------------------------------
    # 手动触发 Review（调试用）
    # ------------------------------------------------------------------

    @app.route("/api/review/run", methods=["POST"])
    def run_review():
        """手动触发一次 Review（默认规则版，可传 mode=llm 强制 LLM 版）。"""
        data = request.json or {}
        try:
            from nbot.review.models import ReviewInput
            from nbot.review.pipeline import ReviewPipeline, get_review_mode

            mode = data.get("mode") or get_review_mode(getattr(server, "data_dir", "data/web"))
            inp = ReviewInput(
                conversation_id=data.get("conversation_id", ""),
                character_id=data.get("character_id", ""),
                user_id=data.get("user_id", ""),
                user_message=data.get("user_message", ""),
                assistant_message=data.get("assistant_message", ""),
                recent_messages=data.get("recent_messages", []),
                selected_choice=data.get("selected_choice"),
                relationship_state=data.get("relationship_state", {}),
                plot_mode=bool(data.get("plot_mode")),
            )
            pipeline = ReviewPipeline(mode=mode)
            output = pipeline.run(inp)
            return jsonify(output.to_dict())
        except Exception as exc:
            _log.error("[ReviewRoutes] run_review failed: %s", exc)
            return jsonify({"error": str(exc)}), 500
