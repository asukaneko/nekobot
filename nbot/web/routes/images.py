"""
生图实验室 API（P2-10 图生图）

统一入口：文生图 / 图生图（参考图 + 重绘强度 + 风格 + 尺寸）、多图批量、
参考图库、生成历史（列表/详情/删除/重试）、配额查询。

依赖 nbot/services/image_service.py 的 generate_image / retry_image_generation。
"""

import logging
import os
import time
import uuid

from flask import jsonify, request
from werkzeug.utils import secure_filename

from nbot.services import image_service

_log = logging.getLogger(__name__)

# 允许上传的参考图扩展名与魔数（只做粗校验，避免把非图片写进静态目录）
_ALLOWED_REFERENCE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_IMAGE_MAGIC = (
    b"\x89PNG\r\n\x1a\n",
    b"\xff\xd8\xff",
    b"GIF87a",
    b"GIF89a",
    b"BM",
)


def _looks_like_image(data: bytes) -> bool:
    """粗略判断字节流是否为常见位图（WebP 单独判断 RIFF....WEBP）。"""
    if not data:
        return False
    if any(data.startswith(magic) for magic in _IMAGE_MAGIC):
        return True
    return data[:4] == b"RIFF" and data[8:12] == b"WEBP"


def _reference_url(filename: str) -> str:
    """参考图对外 URL。"""
    return f"/static/uploads/image_lab/references/{filename}"


def _reference_file_path(filename: str) -> str | None:
    """参考图本地路径；文件名非法或越界返回 None。"""
    safe_name = os.path.basename(filename or "")
    if not safe_name or safe_name != filename:
        return None
    return os.path.join(image_service.get_image_reference_dir(), safe_name)


def register_image_routes(app, server):
    """注册生图实验室路由。"""

    @app.after_request
    def _image_security_headers(response):
        """生图静态资源加 nosniff，避免被当成 HTML/脚本解析。"""
        if request.path.startswith("/static/uploads/image_lab/"):
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
        return response

    @app.route("/api/images/styles", methods=["GET"])
    def image_styles_route():
        """风格 / 尺寸预设 + 生成限制，供前端渲染表单。"""
        try:
            quota = image_service.get_image_quota_settings()
            reference_limits = image_service.get_reference_limits()
            return jsonify({
                "success": True,
                "styles": image_service.list_image_style_presets(),
                "sizes": image_service.IMAGE_SIZE_PRESETS,
                "defaults": {
                    "mode": "txt2img",
                    "size": image_service.normalize_image_size(""),
                    "strength": image_service.DEFAULT_IMAGE_STRENGTH,
                    "style": "none",
                    "count": 1,
                },
                "limits": {
                    "max_images_per_request": quota["max_images_per_request"],
                    "max_reference_images": reference_limits["max_reference_images"],
                    "max_reference_bytes": reference_limits["max_reference_bytes"],
                    "max_reference_total_bytes": reference_limits["max_reference_total_bytes"],
                    "min_side": quota["min_side"],
                    "max_side": quota["max_side"],
                    "daily_limit": quota["daily_limit"],
                },
                "providers": _image_models(server),
            })
        except Exception as exc:  # noqa: BLE001 - 统一转 500
            _log.error("[Images] styles failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/quota", methods=["GET"])
    def image_quota_route():
        """当日生图配额。"""
        try:
            count = request.args.get("count", "1")
            try:
                requested = max(1, int(count))
            except (TypeError, ValueError):
                requested = 1
            quota = image_service.check_image_quota(requested)
            return jsonify({"success": True, **quota})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] quota failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/generate", methods=["POST"])
    def image_generate_route():
        """生成图片（文生图 / 图生图 / 批量）。"""
        payload = request.get_json(silent=True) or {}
        try:
            config = image_service.resolve_image_generation_config(
                payload.get("model_id", ""), server=server
            )
            if config is None and payload.get("model_id"):
                return jsonify({
                    "success": False,
                    "error": "指定的生图模型不存在或未启用",
                    "reason": "no_model_config",
                }), 400

            result = image_service.generate_image(
                payload.get("prompt", ""),
                config=config,
                mode=payload.get("mode", "txt2img"),
                reference_images=payload.get("reference_images") or [],
                strength=payload.get("strength", image_service.DEFAULT_IMAGE_STRENGTH),
                style=payload.get("style", ""),
                negative_prompt=payload.get("negative_prompt", ""),
                size=payload.get("size", ""),
                count=payload.get("count", 1),
                source=str(payload.get("source") or "web"),
                session_id=str(payload.get("session_id") or ""),
            )
            status = 200
            if not result.get("success"):
                reason = result.get("reason", "")
                status = 429 if reason == "quota_exceeded" else (
                    503 if reason == "no_model_config" else 400
                )
            return jsonify(result), status
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] generate failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/references", methods=["GET"])
    def image_references_route():
        """参考图库列表（按修改时间倒序）。"""
        try:
            directory = image_service.get_image_reference_dir()
            items = []
            if os.path.isdir(directory):
                for name in os.listdir(directory):
                    path = os.path.join(directory, name)
                    if not os.path.isfile(path):
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in _ALLOWED_REFERENCE_EXTS:
                        continue
                    stat = os.stat(path)
                    items.append({
                        "name": name,
                        "url": _reference_url(name),
                        "bytes": stat.st_size,
                        "created_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%S", time.localtime(stat.st_mtime)
                        ),
                    })
            items.sort(key=lambda item: item["created_at"], reverse=True)
            return jsonify({"success": True, "references": items, "total": len(items)})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] list references failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/references", methods=["POST"])
    def image_reference_upload_route():
        """上传参考图（multipart: file），返回静态 URL。"""
        try:
            upload = request.files.get("file")
            if upload is None:
                return jsonify({"success": False, "error": "file 必填"}), 400

            filename = upload.filename or "reference.png"
            ext = os.path.splitext(filename)[1].lower()
            if ext not in _ALLOWED_REFERENCE_EXTS:
                return jsonify({
                    "success": False,
                    "error": f"不支持的图片格式：{ext or '未知'}",
                }), 400

            data = upload.read()
            limits = image_service.get_reference_limits()
            if len(data) > limits["max_reference_bytes"]:
                return jsonify({
                    "success": False,
                    "error": f"参考图过大：{len(data)} 字节（上限 {limits['max_reference_bytes']}）",
                }), 400
            if not _looks_like_image(data):
                return jsonify({"success": False, "error": "文件内容不是有效图片"}), 400

            stem = secure_filename(os.path.splitext(filename)[0])[:24] or "ref"
            name = f"{stem}_{int(time.time())}_{uuid.uuid4().hex[:6]}{ext}"
            directory = image_service.get_image_reference_dir()
            os.makedirs(directory, exist_ok=True)
            filepath = os.path.join(directory, name)
            with open(filepath, "wb") as handle:
                handle.write(data)

            return jsonify({
                "success": True,
                "reference": {
                    "name": name,
                    "url": _reference_url(name),
                    "path": image_service.static_relative_path(filepath),
                    "bytes": len(data),
                },
                "quota": image_service.get_image_quota_settings(),
                "limits": limits,
            })
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] upload reference failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/references/<path:filename>", methods=["DELETE"])
    def image_reference_delete_route(filename):
        """删除参考图库里的图片。"""
        try:
            path = _reference_file_path(filename)
            if not path or not os.path.isfile(path):
                return jsonify({"success": False, "error": "参考图不存在"}), 404
            os.unlink(path)
            return jsonify({"success": True})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] delete reference failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    # ------------------------------------------------------------------
    # 生图实验室：模板 / 参数组合对比 / 任务队列
    # ------------------------------------------------------------------

    @app.route("/api/images/templates", methods=["GET"])
    def image_templates_route():
        """模板列表（内置 + 用户自存）。"""
        try:
            result = image_service.list_image_lab_templates()
            return jsonify({"success": True, **result})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] list templates failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/templates", methods=["POST"])
    def image_template_create_route():
        """新建用户模板。"""
        payload = request.get_json(silent=True) or {}
        try:
            template = image_service.save_image_lab_template(payload)
            return jsonify({"success": True, "template": template}), 201
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] create template failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/templates/<template_id>", methods=["PUT"])
    def image_template_update_route(template_id):
        """更新用户模板。"""
        payload = request.get_json(silent=True) or {}
        try:
            template = image_service.update_image_lab_template(template_id, payload)
            return jsonify({"success": True, "template": template})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except KeyError:
            return jsonify({"success": False, "error": "模板不存在"}), 404
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] update template failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/templates/<template_id>", methods=["DELETE"])
    def image_template_delete_route(template_id):
        """删除用户模板。"""
        try:
            if not image_service.delete_image_lab_template(template_id):
                return jsonify({"success": False, "error": "模板不存在"}), 404
            return jsonify({"success": True, "id": template_id})
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] delete template failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/lab/plan", methods=["POST"])
    def image_lab_plan_route():
        """参数组合预览：只算出 groups，不生成图片。"""
        payload = request.get_json(silent=True) or {}
        try:
            explicit = payload.get("variants") or None
            base = image_service._lab_base_params(payload, allow_empty_prompt=bool(explicit))
            plan = image_service.build_compare_groups(
                base,
                payload.get("variations") or {},
                explicit=explicit,
                limit=payload.get("limit") or image_service._MAX_COMPARE_GROUPS,
            )
            total_images = sum(int(group["params"]["count"]) for group in plan["groups"])
            return jsonify({
                "success": True,
                "base": {key: value for key, value in base.items() if key != "template_id"},
                "groups": plan["groups"],
                "total": plan["total"],
                "truncated": plan["truncated"],
                "requested_images": total_images,
                "quota": image_service.check_image_quota(total_images),
            })
        except (ValueError, KeyError) as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] lab plan failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/lab/tasks", methods=["POST"])
    def image_lab_task_create_route():
        """提交异步任务（单组或多组参数对比）。"""
        payload = request.get_json(silent=True) or {}
        try:
            result = image_service.submit_image_lab_task(payload)
            if not result.get("success"):
                reason = result.get("reason", "")
                status = 429 if reason == "quota_exceeded" else (
                    503 if reason == "no_model_config" else 400
                )
                return jsonify(result), status
            return jsonify(result), 202
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] create lab task failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/lab/tasks", methods=["GET"])
    def image_lab_task_list_route():
        """任务列表（摘要，最新在前）。"""
        try:
            result = image_service.list_image_lab_tasks(request.args.get("limit", 20))
            return jsonify({"success": True, **result})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] list lab tasks failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/lab/tasks/<task_id>", methods=["GET"])
    def image_lab_task_detail_route(task_id):
        """任务详情：进度 + 分组结果（groups）。"""
        try:
            task = image_service.get_image_lab_task(task_id)
            if not task:
                return jsonify({"success": False, "error": "任务不存在"}), 404
            return jsonify({"success": True, "task": task})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] lab task detail failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/lab/tasks/<task_id>/cancel", methods=["POST"])
    def image_lab_task_cancel_route(task_id):
        """取消排队中/执行中的任务。"""
        try:
            if not image_service.cancel_image_lab_task(task_id):
                return jsonify({"success": False, "error": "任务不存在或已结束"}), 404
            return jsonify({"success": True, "id": task_id})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] cancel lab task failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/history", methods=["GET"])
    def image_history_route():
        """生成历史分页列表。"""
        try:
            result = image_service.list_image_history(
                mode=request.args.get("mode", ""),
                keyword=request.args.get("keyword", ""),
                status=request.args.get("status", ""),
                limit=request.args.get("limit", 20),
                offset=request.args.get("offset", 0),
            )
            return jsonify({"success": True, **result})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] history list failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/history/<entry_id>", methods=["GET"])
    def image_history_detail_route(entry_id):
        """生成历史详情（含全部结果路径）。"""
        try:
            entry = image_service.get_image_history_entry(entry_id)
            if not entry:
                return jsonify({"success": False, "error": "记录不存在"}), 404
            return jsonify({"success": True, "entry": entry})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] history detail failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/history/<entry_id>", methods=["DELETE"])
    def image_history_delete_route(entry_id):
        """删除历史记录（默认连带删除结果文件）。"""
        try:
            keep_files = request.args.get("keep_files", "0") in ("1", "true", "yes")
            removed = image_service.delete_image_history_entry(
                entry_id, delete_files=not keep_files
            )
            if not removed:
                return jsonify({"success": False, "error": "记录不存在"}), 404
            return jsonify({"success": True, "id": entry_id})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] history delete failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/history/bulk-delete", methods=["POST"])
    def image_history_bulk_delete_route():
        """批量删除历史记录：{"ids": [...], "keep_files": false}。"""
        try:
            payload = request.get_json(silent=True) or {}
            ids = payload.get("ids") or []
            if not isinstance(ids, list) or not ids:
                return jsonify({"success": False, "error": "ids 必填"}), 400
            keep_files = bool(payload.get("keep_files"))
            deleted, missing = [], []
            for entry_id in ids:
                if image_service.delete_image_history_entry(
                    str(entry_id), delete_files=not keep_files
                ):
                    deleted.append(str(entry_id))
                else:
                    missing.append(str(entry_id))
            return jsonify({"success": True, "deleted": deleted, "missing": missing})
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] bulk delete failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/images/history/<entry_id>/retry", methods=["POST"])
    def image_history_retry_route(entry_id):
        """按历史参数重试生成。"""
        try:
            result = image_service.retry_image_generation(entry_id, source="web_retry")
            status = 200 if result.get("success") else (
                429 if result.get("reason") == "quota_exceeded" else 400
            )
            return jsonify(result), status
        except Exception as exc:  # noqa: BLE001
            _log.error("[Images] retry failed: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": str(exc)}), 500


def _image_models(server) -> list[dict]:
    """列出启用中的图片生成模型（供前端选择 model_id）。"""
    models = []
    for model in getattr(server, "ai_models", []) or []:
        if model.get("purpose") != "image_generation":
            continue
        if not model.get("enabled", True):
            continue
        models.append({
            "id": model.get("id", ""),
            "name": model.get("name") or model.get("model", ""),
            "model": model.get("model", ""),
            "provider": model.get("provider") or model.get("provider_type", ""),
            "active": model.get("id") == (
                (getattr(server, "active_models_by_purpose", {}) or {}).get("image_generation")
            ),
        })
    return models
