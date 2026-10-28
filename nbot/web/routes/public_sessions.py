# -*- coding: utf-8 -*-
"""Public session API."""

import hashlib
import logging
import os
import time
from datetime import datetime

from flask import jsonify, render_template, request

from nbot.core.session_store import visible_messages
from nbot.web.secure_store import read_secure_json, write_secure_json

_log = logging.getLogger(__name__)

_public_sessions = {}
_public_sessions_loaded = False


def _get_public_sessions_file(data_dir):
    return os.path.join(data_dir, "public_sessions.json")


def _load_public_sessions(data_dir):
    global _public_sessions, _public_sessions_loaded
    if _public_sessions_loaded:
        return

    file_path = _get_public_sessions_file(data_dir)
    if os.path.exists(file_path):
        try:
            data, _ = read_secure_json(file_path, data_dir, {})
            if isinstance(data, dict):
                now = time.time()
                _public_sessions = {
                    public_id: info
                    for public_id, info in data.items()
                    if info.get("expires_at", 0) > now
                }
                _log.info("[PublicSession] loaded %d public sessions", len(_public_sessions))
        except Exception as exc:
            _log.error("[PublicSession] failed to load public sessions: %s", exc)
            _public_sessions = {}
    else:
        _public_sessions = {}

    _public_sessions_loaded = True


def _save_public_sessions(data_dir):
    try:
        write_secure_json(_get_public_sessions_file(data_dir), data_dir, _public_sessions)
    except Exception as exc:
        _log.error("[PublicSession] failed to save public sessions: %s", exc)


def _generate_public_id(session_id):
    return hashlib.sha256(f"nbot_public:{session_id}".encode()).hexdigest()[:16]


def _hash_public_password(public_id, password):
    password = str(password or "")
    if not password:
        return ""
    return hashlib.sha256(f"nbot_public_password:{public_id}:{password}".encode()).hexdigest()


def _verify_public_password(info, password):
    password_hash = info.get("password_hash") or ""
    if not password_hash:
        return True
    return _hash_public_password(info.get("public_id", ""), password) == password_hash


def _int_or_none(value):
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_public_options(data):
    data = data or {}
    expires_days = _int_or_none(data.get("expires_days"))
    if expires_days is None:
        expires_days = 30
    expires_days = max(1, min(expires_days, 365))
    return {
        "expires_days": expires_days,
        "include_character": bool(data.get("include_character", True)),
        "include_user_messages": bool(data.get("include_user_messages", True)),
        "message_start": _int_or_none(data.get("message_start")),
        "message_end": _int_or_none(data.get("message_end")),
        "password_required": bool(str(data.get("password") or "").strip()),
    }


def _build_public_messages(session, options):
    public_messages = []
    for msg in visible_messages(session.get("messages", [])):
        role = msg.get("role")
        if role == "system":
            continue
        if role == "user" and not options.get("include_user_messages", True):
            continue
        public_messages.append({
            "id": msg.get("id", ""),
            "role": role or "",
            "content": msg.get("content", ""),
            "timestamp": msg.get("timestamp", ""),
            "sender": msg.get("sender", ""),
        })

    start = options.get("message_start")
    end = options.get("message_end")
    if start is not None or end is not None:
        start_idx = max((start or 1) - 1, 0)
        end_idx = max(end, start_idx) if end is not None else len(public_messages)
        public_messages = public_messages[start_idx:end_idx]
    return public_messages


def _build_public_session_data(session, options):
    # Agent 模式会话不携带任何角色卡内容（sender/scenario 等），始终保持纯净对话
    is_agent_session = (session or {}).get("session_mode") == "agent"
    include_character = options.get("include_character", True) and not is_agent_session
    return {
        "name": session.get("name", "未命名会话"),
        "sender_name": session.get("sender_name", "") if include_character else "",
        "sender_avatar": session.get("sender_avatar", "") if include_character else "",
        "sender_portrait": session.get("sender_portrait", "") if include_character else "",
        "scenario": session.get("scenario", "") if include_character else "",
        "created_at": session.get("created_at", ""),
        "messages": _build_public_messages(session, options),
    }


def _cleanup_expired():
    now = time.time()
    expired = [
        public_id
        for public_id, info in _public_sessions.items()
        if info.get("expires_at", 0) < now
    ]
    for public_id in expired:
        del _public_sessions[public_id]
    return len(expired)


def register_public_session_routes(app, server):
    _load_public_sessions(server.data_dir)

    @app.route("/api/sessions/<session_id>/public", methods=["POST"])
    def make_session_public(session_id):
        from nbot.core import WebSessionStore
        from nbot.web.persistence import is_web_visible_session
        from nbot.web.sessions_db import get_session as get_session_from_db

        session_store = WebSessionStore(
            server.sessions, save_callback=lambda: server._save_data("sessions")
        )
        session = session_store.get_session(session_id)
        if not session:
            session = get_session_from_db(server.data_dir, session_id)
        if not session or not is_web_visible_session(session_id, session):
            return jsonify({"error": "Session not found"}), 404

        payload = request.get_json(silent=True) or {}
        public_id = _generate_public_id(session_id)
        options = _normalize_public_options(payload)
        # Agent 模式强制不包含角色内容，无视前端传入的 include_character
        if (session or {}).get("session_mode") == "agent":
            options["include_character"] = False
        password = str(payload.get("password") or "").strip()

        _public_sessions[public_id] = {
            "session_id": session_id,
            "public_id": public_id,
            "created_at": datetime.now().isoformat(),
            "expires_at": time.time() + options["expires_days"] * 24 * 3600,
            "options": options,
            "password_hash": _hash_public_password(public_id, password),
            "session_data": _build_public_session_data(session, options),
        }
        _save_public_sessions(server.data_dir)

        host_url = request.headers.get("Origin", "") or request.host_url.rstrip("/")
        public_url = f"{host_url}/public/{public_id}"
        _log.info("[PublicSession] session %s is public: %s", session_id[:8], public_id)

        return jsonify({
            "success": True,
            "public_id": public_id,
            "public_url": public_url,
            "expires_at": _public_sessions[public_id]["expires_at"],
            "options": options,
            "password_required": bool(_public_sessions[public_id].get("password_hash")),
        })

    @app.route("/api/sessions/<session_id>/public", methods=["DELETE"])
    def remove_session_public(session_id):
        public_id = _generate_public_id(session_id)
        if public_id in _public_sessions:
            del _public_sessions[public_id]
            _save_public_sessions(server.data_dir)
            _log.info("[PublicSession] session %s public share removed", session_id[:8])
        return jsonify({"success": True})

    @app.route("/api/sessions/<session_id>/public/status", methods=["GET"])
    def get_session_public_status(session_id):
        _cleanup_expired()
        public_id = _generate_public_id(session_id)
        is_public = public_id in _public_sessions
        result = {"success": True, "is_public": is_public}
        if is_public:
            info = _public_sessions[public_id]
            host_url = request.headers.get("Origin", "") or request.host_url.rstrip("/")
            result.update({
                "public_url": f"{host_url}/public/{public_id}",
                "public_id": public_id,
                "expires_at": info.get("expires_at"),
                "options": info.get("options", {}),
                "password_required": bool(info.get("password_hash")),
            })
        return jsonify(result)

    @app.route("/public/<public_id>")
    def view_public_session(public_id):
        _cleanup_expired()
        if public_id not in _public_sessions:
            return render_template("public_session.html", error="该公开链接已失效或不存在"), 404

        info = _public_sessions[public_id]
        password = request.args.get("password", "")
        if not _verify_public_password(info, password):
            return render_template(
                "public_session.html",
                error=None,
                password_required=True,
                password_error=bool(password),
                public_id=public_id,
            )

        session_data = info.get("session_data", {})
        return render_template(
            "public_session.html",
            error=None,
            password_required=False,
            session_name=session_data.get("name", "未命名会话"),
            sender_name=session_data.get("sender_name", ""),
            sender_avatar=session_data.get("sender_avatar", ""),
            sender_portrait=session_data.get("sender_portrait", ""),
            scenario=session_data.get("scenario", ""),
            created_at=session_data.get("created_at", ""),
            messages=session_data.get("messages", []),
        )

    @app.route("/api/public/<public_id>")
    def get_public_session_data(public_id):
        _cleanup_expired()
        if public_id not in _public_sessions:
            return jsonify({"error": "Public session not found"}), 404

        info = _public_sessions[public_id]
        if not _verify_public_password(info, request.args.get("password", "")):
            return jsonify({"error": "Password required"}), 401
        return jsonify({
            "success": True,
            "session_data": info.get("session_data", {}),
        })
