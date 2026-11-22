from datetime import datetime

from flask import jsonify, request, send_file

from nbot.web.config_transfer import (
    ConfigTransferError,
    apply_bundle,
    build_zip_bundle,
    decrypt_bundle,
    load_bundle_bytes_with_portraits,
    restore_portraits_from_zip,
    sniff_bundle_format,
)


def register_config_transfer_routes(app, server):
    @app.route("/api/config-transfer/export", methods=["POST"])
    def export_config_bundle():
        """导出配置为 ZIP 文件（含加密配置 + 角色立绘）"""
        data = request.json or {}
        password = str(data.get("password") or "").strip()
        try:
            zip_buffer = build_zip_bundle(server, password)
        except ConfigTransferError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400

        filename = f"nbot-config-{datetime.now().strftime('%Y%m%d-%H%M%S')}.zip"
        return send_file(
            zip_buffer,
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/api/config-transfer/import", methods=["POST"])
    def import_config_bundle():
        """导入配置，支持 ZIP（含立绘）与 .nbotcfg（JSON / 裸二进制）等格式。

        格式按文件头嗅探，不依赖扩展名：客户端把 ZIP 命名成 .nbotcfg、
        把二进制密文命名成 .zip，或用 GZIP 包裹 ZIP，都能正确导入。
        """
        password = ""
        overwrite = True

        if request.files:
            upload = request.files.get("file")
            password = str(request.form.get("password") or "").strip()
            overwrite = str(request.form.get("overwrite", "true")).lower() != "false"
            if not upload:
                return jsonify({"success": False, "error": "未选择配置包文件"}), 400

            raw_bytes = upload.read()
            if not raw_bytes:
                return jsonify({"success": False, "error": "配置包文件为空"}), 400

            return _handle_bundle_import(server, raw_bytes, password, overwrite)

        data = request.json or {}
        password = str(data.get("password") or "").strip()
        overwrite = bool(data.get("overwrite", True))
        raw_bundle = data.get("bundle")
        try:
            bundle = decrypt_bundle(raw_bundle, password)
            result = apply_bundle(server, bundle, overwrite=overwrite)
            server.log_message(
                "info",
                f"已导入配置包，配置项 {len(result['imported'])} 个",
                important=True,
            )
            return jsonify({"success": True, **result})
        except ConfigTransferError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"success": False, "error": f"导入失败: {exc}"}), 500


def _handle_bundle_import(server, raw_bytes: bytes, password: str, overwrite: bool):
    """处理单文件配置导入（JSON / 裸二进制 / GZIP / ZIP，按文件头嗅探）。

    ZIP 及嵌套 ZIP（含 GZIP 包裹）中的立绘会一并恢复，避免
    「配置导入成功但 portrait 引用指向不存在的文件」。
    """
    try:
        bundle, portraits = load_bundle_bytes_with_portraits(raw_bytes, password)
    except ConfigTransferError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    # 恢复立绘到本地（会修改 bundle 中的 URL 引用）
    restored_count = 0
    if portraits:
        try:
            restored_count = restore_portraits_from_zip(server, portraits, bundle)
        except Exception as exc:
            server.log_message("warning", f"立绘恢复部分失败: {exc}")

    try:
        result = apply_bundle(server, bundle, overwrite=overwrite)
    except ConfigTransferError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "error": f"导入失败: {exc}"}), 500

    log_msg = (
        f"已导入配置包 ({sniff_bundle_format(raw_bytes)})，"
        f"配置项 {len(result['imported'])} 个"
    )
    if restored_count:
        log_msg += f"，立绘 {restored_count} 张"
    server.log_message("info", log_msg, important=True)

    resp_data = {"success": True, **result}
    if restored_count > 0:
        resp_data["portraits_restored"] = restored_count
    return jsonify(resp_data)


def _handle_zip_import(server, raw_bytes: bytes, password: str, overwrite: bool):
    """兼容旧调用名：ZIP 与非 ZIP 现由同一个加载器处理。"""
    return _handle_bundle_import(server, raw_bytes, password, overwrite)


def _handle_json_import(server, raw_bytes: bytes, password: str, overwrite: bool):
    """兼容旧调用名：单文件导入（JSON / 裸二进制 / GZIP / ZIP）。"""
    return _handle_bundle_import(server, raw_bytes, password, overwrite)
