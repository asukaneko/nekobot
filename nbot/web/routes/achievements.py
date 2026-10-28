"""
成就系统 Web API

路由：
    GET    /api/achievements            — 成就列表（含进度/解锁状态）
    GET    /api/achievements/summary    — 成就概览
    POST   /api/achievements/refresh    — 重新计算进度（读取外部统计快照）
    POST   /api/achievements/reset      — 重置所有成就
    GET    /api/achievements/stats      — 外部统计快照（调试用）
"""

from __future__ import annotations

import json
import logging
import os

from flask import jsonify

_log = logging.getLogger(__name__)


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read_json_file(path: str, default):
    try:
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _snapshot_from_server(server) -> dict:
    """从服务器状态聚合外部统计快照。"""
    data_dir = getattr(server, "data_dir", "data/web") or "data/web"
    root = _project_root()

    def _path(rel: str) -> str:
        return os.path.join(root, "data", rel)

    # 角色卡
    presets = _read_json_file(
        os.path.join(data_dir, "custom_personality_presets.json"), []
    )
    characters = len(presets) if isinstance(presets, list) else 0

    # 世界书
    world_books = _read_json_file(os.path.join(data_dir, "world_books.json"), {})
    if isinstance(world_books, dict):
        wb = world_books.get("world_books", world_books)
        worldbooks = len(wb) if isinstance(wb, (dict, list)) else 0
    elif isinstance(world_books, list):
        worldbooks = len(world_books)
    else:
        worldbooks = 0

    # MCP 服务器
    mcp = _read_json_file(os.path.join(data_dir, "mcp_servers.json"), [])
    mcp_servers = len(mcp) if isinstance(mcp, list) else (len(mcp.get("servers", [])) if isinstance(mcp, dict) else 0)

    # Skills
    skills_data = _read_json_file(os.path.join(data_dir, "skills.json"), {})
    if isinstance(skills_data, list):
        skills = len(skills_data)
    elif isinstance(skills_data, dict):
        skills = len(skills_data.get("skills", [])) if isinstance(skills_data.get("skills"), list) else 0
    else:
        skills = 0

    # Hooks
    hooks = _read_json_file(os.path.join(data_dir, "hooks.json"), [])
    hook_count = len(hooks) if isinstance(hooks, list) else 0

    # OAuth 账户
    oauth_data = _read_json_file(os.path.join(data_dir, "oauth_accounts.json"), {})
    if isinstance(oauth_data, dict):
        oauth_accounts = len(oauth_data.get("accounts", [])) if isinstance(oauth_data.get("accounts"), list) else 0
    elif isinstance(oauth_data, list):
        oauth_accounts = len(oauth_data)
    else:
        oauth_accounts = 0

    # 好感度（跨角色最大）
    affection_max = 0.0
    try:
        from nbot.character.storage.json_store import RelationshipJsonStore
        rel_store = RelationshipJsonStore(root)
        for rel in rel_store.list_all() or []:
            try:
                affection_max = max(affection_max, float(rel.get("affection") or 0))
            except (TypeError, ValueError):
                continue
    except Exception:
        pass

    # 剧情节点
    plot_nodes = 0
    try:
        from nbot.plot.graph_manager import PlotGraphManager
        gm = PlotGraphManager(data_dir)
        plot_nodes = len(getattr(gm, "_nodes", {}) or {})
    except Exception:
        pass

    # 记忆
    memories = 0
    try:
        from nbot.memory.fs import get_memory_fs
        mfs = get_memory_fs()
        memories = len(getattr(mfs, "_index", {}) or {})
    except Exception:
        pass

    # 会话
    sessions = 0
    try:
        sessions_store = getattr(server, "sessions", None)
        if isinstance(sessions_store, dict):
            sessions = len(sessions_store)
        else:
            from nbot.web.sessions_db import load_sessions
            sessions = len(load_sessions(data_dir))
    except Exception:
        pass

    # WebDAV 备份
    webdav_backup = 0
    try:
        from nbot.web.webdav_backup import get_config
        cfg = get_config(server)
        if cfg.get("enabled") and cfg.get("last_backup_at"):
            webdav_backup = 1
    except Exception:
        pass

    # 故障转移模型数（启用且按优先级排序的 chat 模型）
    failover_models = 0
    try:
        from nbot.web.utils.config_loader import get_model_configs_by_purpose
        cfgs = get_model_configs_by_purpose("chat")
        failover_models = len(cfgs) if cfgs else 0
    except Exception:
        pass

    # 智能路由开关
    smart_routing = 0
    try:
        settings = getattr(server, "settings", {}) or {}
        sr = (settings.get("smart_routing") or {}) if isinstance(settings, dict) else {}
        smart_routing = 1 if (isinstance(sr, dict) and sr.get("enabled")) else 0
    except Exception:
        pass

    return {
        "characters": characters,
        "worldbooks": worldbooks,
        "mcp_servers": mcp_servers,
        "skills": skills,
        "hooks": hook_count,
        "oauth_accounts": oauth_accounts,
        "affection_max": affection_max,
        "plot_nodes": plot_nodes,
        "memories": memories,
        "sessions": sessions,
        "webdav_backup": webdav_backup,
        "failover_models": failover_models,
        "smart_routing": smart_routing,
    }


def register_achievement_routes(app, server):

    @app.route("/api/achievements")
    def list_achievements():
        from nbot.achievements.manager import get_achievement_manager
        mgr = get_achievement_manager(getattr(server, "data_dir", "data/web"))
        mgr.refresh(_snapshot_from_server(server))
        return jsonify({"achievements": mgr.list_all(), "summary": mgr.summary()})

    @app.route("/api/achievements/summary")
    def achievement_summary():
        from nbot.achievements.manager import get_achievement_manager
        mgr = get_achievement_manager(getattr(server, "data_dir", "data/web"))
        mgr.refresh(_snapshot_from_server(server))
        return jsonify(mgr.summary())

    @app.route("/api/achievements/refresh", methods=["POST"])
    def refresh_achievements():
        from nbot.achievements.manager import get_achievement_manager
        mgr = get_achievement_manager(getattr(server, "data_dir", "data/web"))
        newly = mgr.refresh(_snapshot_from_server(server))
        return jsonify({"ok": True, "newly_unlocked": newly, "summary": mgr.summary()})

    @app.route("/api/achievements/reset", methods=["POST"])
    def reset_achievements():
        from nbot.achievements.manager import get_achievement_manager
        mgr = get_achievement_manager(getattr(server, "data_dir", "data/web"))
        mgr.reset()
        return jsonify({"ok": True})

    @app.route("/api/achievements/stats")
    def achievement_stats():
        return jsonify(_snapshot_from_server(server))
