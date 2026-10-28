"""WebDAV 备份/同步相关的 HTTP 路由。

包含：连接与配置、覆盖式备份、增量修订（列表/下载/删除/指定版本恢复）、
TTS 缓存与表情包资产同步。
"""

import io

from flask import jsonify, request, send_file

from nbot.web.webdav_backup import (
    STICKERS_SUBDIR,
    TTS_CACHE_SUBDIR,
    WebDAVBackupError,
    delete_revision,
    download_revision,
    get_config,
    get_remote_info,
    list_revisions,
    pull_backup,
    restore_revision,
    save_config,
    sync_assets_download,
    sync_assets_upload,
    test_connection,
    upload_backup,
)

_ASSET_KINDS = (TTS_CACHE_SUBDIR, STICKERS_SUBDIR)


def _optional_bool(data: dict, key: str):
    """缺失返回 None（表示跟随配置开关），显式传值才转 bool。"""
    if key not in data or data.get(key) is None:
        return None
    return bool(data.get(key))


def register_webdav_backup_routes(app, server):
    @app.route("/api/webdav/config")
    def webdav_get_config():
        """读取 WebDAV 备份配置（密码字段会被脱敏）。"""
        return jsonify(get_config(server))

    @app.route("/api/webdav/config", methods=["PUT"])
    def webdav_save_config():
        """保存 WebDAV 备份配置。

        密码字段为空字符串或形如 "ab****cd" 时视为不修改；
        想清空密码时需额外传 clear_password=true / clear_encryption_password=true。
        支持 retention{enabled,max_count,max_days} 与 sync_tts_cache / sync_stickers 开关。
        """
        data = request.json or {}
        try:
            config = save_config(server, data)
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, "config": config})

    @app.route("/api/webdav/test", methods=["POST"])
    def webdav_test():
        """测试 WebDAV 连接。

        请求体可选:
          - url, username, password: 临时覆盖配置进行测试（不持久化）
        """
        data = request.json or {}
        override = None
        if data.get("url") or data.get("username") or data.get("password"):
            current = (
                server.settings.get("webdav_backup") or {}
                if isinstance(server.settings, dict)
                else {}
            )
            override = {
                "url": str(data.get("url") or current.get("url") or ""),
                "username": str(data.get("username") or current.get("username") or ""),
                "password": str(data.get("password") or current.get("password") or ""),
            }
        try:
            result = test_connection(server, config_override=override)
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, **result})

    @app.route("/api/webdav/info")
    def webdav_info():
        """查询远程最新备份文件元信息。"""
        try:
            info = get_remote_info(server)
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, **info})

    @app.route("/api/webdav/revisions")
    def webdav_revisions():
        """列出版本历史（manifest 为准，缺失回退 PROPFIND）。"""
        try:
            payload = list_revisions(server)
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, **payload})

    @app.route("/api/webdav/revisions/<revision_id>/download")
    def webdav_revision_download(revision_id):
        """下载指定版本（原始 .nbotcfg 字节）。"""
        try:
            payload = download_revision(server, revision_id)
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return send_file(
            io.BytesIO(payload["content"]),
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=f"nekobot-config-{payload['id']}.nbotcfg",
        )

    @app.route("/api/webdav/revisions/<revision_id>", methods=["DELETE"])
    def webdav_revision_delete(revision_id):
        """删除指定版本（文件 + manifest 条目）。"""
        try:
            result = delete_revision(server, revision_id)
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, **result})

    @app.route("/api/webdav/revisions/<revision_id>/restore", methods=["POST"])
    def webdav_revision_restore(revision_id):
        """把本地配置恢复到指定版本。

        请求体可选:
          - password: 临时加密密码（不持久化）
        """
        data = request.json or {}
        password_override = str(data.get("password") or "").strip() or None
        try:
            result = restore_revision(
                server, revision_id, password_override=password_override
            )
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, **result})

    @app.route("/api/webdav/assets/sync", methods=["POST"])
    def webdav_assets_sync():
        """同步 TTS 缓存 / 表情包。

        请求体可选:
          - direction: "upload"（默认）或 "download"
          - kinds: ["tts_cache", "stickers"]，缺省时按配置开关
          - include_tts_cache / include_stickers: 覆盖配置开关
        """
        data = request.json or {}
        direction = str(data.get("direction") or "upload").strip().lower()
        if direction not in ("upload", "download"):
            return (
                jsonify({"success": False, "error": "direction 只能是 upload 或 download"}),
                400,
            )

        raw_kinds = data.get("kinds")
        kinds = []
        if isinstance(raw_kinds, list):
            for item in raw_kinds:
                kind = str(item or "").strip()
                if kind in _ASSET_KINDS and kind not in kinds:
                    kinds.append(kind)
        if not kinds:
            config = get_config(server)
            if config.get("sync_tts_cache"):
                kinds.append(TTS_CACHE_SUBDIR)
            if config.get("sync_stickers"):
                kinds.append(STICKERS_SUBDIR)
        if not kinds:
            return (
                jsonify(
                    {
                        "success": False,
                        "error": "未选择要同步的资产（TTS 缓存 / 表情包）",
                    }
                ),
                400,
            )

        results = {}
        for kind in kinds:
            try:
                if direction == "upload":
                    results[kind] = sync_assets_upload(server, kind)
                else:
                    results[kind] = sync_assets_download(server, kind)
            except WebDAVBackupError as exc:
                results[kind] = {"ok": False, "error": str(exc)}
        return jsonify({"success": True, "direction": direction, "assets": results})

    @app.route("/api/webdav/backup", methods=["POST"])
    def webdav_backup():
        """构建加密 .nbotcfg 上传，并另存一份增量修订。

        请求体可选:
          - password: 临时加密密码（不持久化）
          - include_portraits: 是否同时上传立绘（默认 false）
          - include_tts_cache / include_stickers: 覆盖配置里的资产同步开关
          - note: 记在修订条目上的备注（如 manual / auto）
        """
        data = request.json or {}
        password_override = str(data.get("password") or "").strip() or None
        include_portraits = bool(data.get("include_portraits"))
        note = str(data.get("note") or "").strip()
        try:
            result = upload_backup(
                server,
                password_override=password_override,
                include_portraits=include_portraits,
                include_tts_cache=_optional_bool(data, "include_tts_cache"),
                include_stickers=_optional_bool(data, "include_stickers"),
                note=note,
            )
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, **result})

    @app.route("/api/webdav/sync", methods=["POST"])
    def webdav_sync():
        """从 WebDAV 拉取配置包并应用到本地（可指定版本）。

        请求体可选:
          - password: 临时加密密码（不持久化）
          - revision_id: 指定版本 id（缺省拉最新）
          - include_portraits: 是否同时拉取立绘（默认 false）
          - include_tts_cache / include_stickers: 覆盖配置里的资产同步开关
        """
        data = request.json or {}
        password_override = str(data.get("password") or "").strip() or None
        include_portraits = bool(data.get("include_portraits"))
        revision_id = str(data.get("revision_id") or "").strip() or None
        try:
            result = pull_backup(
                server,
                password_override=password_override,
                include_portraits=include_portraits,
                revision_id=revision_id,
                include_tts_cache=_optional_bool(data, "include_tts_cache"),
                include_stickers=_optional_bool(data, "include_stickers"),
            )
        except WebDAVBackupError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        return jsonify({"success": True, **result})
