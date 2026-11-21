"""会话导入路由：导入格式列表、DeepSeek 官方归档的扫描 / 导入 / 丢弃。

流程（与客户端一致）::

    选择格式 → 选择归档（本地 ZIP 或下载链接）→ 勾选会话 → 导入

扫描与导入拆成两次请求：DeepSeek 导出包可能很大，服务端把 ZIP 暂存到临时目录并返回
一次性 token，避免把同一份文件上传两次。token 只保存在内存里，过期（默认 30 分钟）
或进程重启后需要重新上传。
"""

from __future__ import annotations

import logging
from typing import Any

from flask import jsonify, request

from nbot.core import WebSessionStore
from nbot.web.session_import import (
    DeepSeekArchiveReader,
    SessionImportError,
    get_staging,
    list_session_import_formats,
)
from nbot.web.session_import.payload import import_export_payload

_log = logging.getLogger(__name__)

#: 单次导入的会话数上限：避免一个请求写入过多会话导致前端超时。
MAX_IMPORT_PER_REQUEST = 500


def register_session_import_routes(app, server):
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )

    def _error(message: str, status: int = 400):
        return jsonify({"success": False, "error": message}), status

    @app.route("/api/sessions/import/formats", methods=["GET"])
    def session_import_formats():
        return jsonify({"success": True, "formats": list_session_import_formats()})

    @app.route("/api/sessions/import/deepseek/scan", methods=["POST"])
    def session_import_deepseek_scan():
        """接收 DeepSeek 导出 ZIP（上传或链接），返回可勾选的会话列表。"""
        staging = get_staging()
        staging.cleanup()

        upload = request.files.get("file") if request.files else None
        try:
            if upload is not None:
                staged = staging.stage_upload(upload)
            else:
                payload = request.get_json(silent=True) or {}
                url = str(payload.get("url") or "").strip()
                if not url:
                    return _error("请上传 DeepSeek 导出 ZIP，或提供下载链接")
                staged = staging.stage_download(url)
        except SessionImportError as exc:
            return _error(str(exc))
        except Exception as exc:
            _log.error("[SessionImport] 归档暂存失败: %s", exc)
            return _error(f"归档读取失败：{exc}", 500)

        try:
            with DeepSeekArchiveReader.open(staged.path) as reader:
                conversations = [item.to_dict() for item in reader.list_conversations()]
                format_version = reader.export_format_version
        except SessionImportError as exc:
            staging.discard(staged.token)
            return _error(str(exc))
        except Exception as exc:
            staging.discard(staged.token)
            _log.error("[SessionImport] DeepSeek 归档解析失败: %s", exc)
            return _error(f"归档解析失败：{exc}", 500)

        if not conversations:
            staging.discard(staged.token)
            return _error("归档里没有可导入的会话")

        _log.info(
            "[SessionImport] DeepSeek 归档扫描完成: %s（%s 个会话）",
            staged.name,
            len(conversations),
        )
        return jsonify(
            {
                "success": True,
                "format": "deepseek",
                "token": staged.token,
                "archive": {"name": staged.name, "size": staged.size},
                "format_version": format_version,
                "total": len(conversations),
                "conversations": conversations,
            }
        )

    @app.route("/api/sessions/import/deepseek/import", methods=["POST"])
    def session_import_deepseek_import():
        """把勾选的会话写入会话库。"""
        staging = get_staging()
        payload: Any = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return _error("请求体必须是 JSON 对象")

        token = str(payload.get("token") or "").strip()
        if not token:
            return _error("缺少导入 token，请先扫描归档")
        raw_ids = payload.get("ids")
        if raw_ids is not None and not isinstance(raw_ids, list):
            return _error("ids 必须是数组")

        try:
            staged = staging.get(token)
        except SessionImportError as exc:
            return _error(str(exc))

        try:
            with DeepSeekArchiveReader.open(staged.path) as reader:
                if raw_ids is None:
                    selected = [item.id for item in reader.list_conversations()]
                else:
                    selected = [str(item).strip() for item in raw_ids]
                    selected = [item for item in selected if item]
                if not selected:
                    return _error("请至少选择一个会话")
                if len(selected) > MAX_IMPORT_PER_REQUEST:
                    return _error(f"单次最多导入 {MAX_IMPORT_PER_REQUEST} 个会话")

                wanted = list(dict.fromkeys(selected))
                built: list[dict] = []
                skipped = 0
                for conversation_id in wanted:
                    session = reader.build_session(conversation_id)
                    if session is None:
                        skipped += 1
                        continue
                    built.append(session)
        except SessionImportError as exc:
            return _error(str(exc))
        except Exception as exc:
            _log.error("[SessionImport] DeepSeek 归档解析失败: %s", exc)
            return _error(f"归档解析失败：{exc}", 500)

        if not built:
            return _error("选中的会话没有可导入的内容")

        result = import_export_payload(server, session_store, built)
        result["success"] = True
        result["skipped"] = skipped
        _log.info(
            "[SessionImport] DeepSeek 导入完成: 成功 %s / 跳过 %s / 失败 %s",
            result["imported"],
            skipped,
            result["failed"],
        )
        log_message = getattr(server, "log_message", None)
        if callable(log_message):
            try:
                log_message(
                    "info",
                    f"从 DeepSeek 导出包导入会话：成功 {result['imported']}，"
                    f"跳过 {skipped}，失败 {result['failed']}",
                    important=True,
                )
            except Exception:  # pragma: no cover - 日志失败不影响导入结果
                pass
        return jsonify(result)

    @app.route("/api/sessions/import/deepseek/discard", methods=["POST"])
    def session_import_deepseek_discard():
        """丢弃暂存的归档（用户关闭弹窗或取消导入时调用）。"""
        payload = request.get_json(silent=True) or {}
        token = payload.get("token") if isinstance(payload, dict) else None
        get_staging().discard(str(token or ""))
        return jsonify({"success": True})
