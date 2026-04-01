import logging
import os

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_auth_routes(app, server):
    @app.route("/")
    def index():
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "index.html"
        )
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html"}

    @app.route("/api/login", methods=["POST"])
    def login():
        data = request.json
        username = data.get("username", "").strip()
        password = data.get("password", "").strip()

        if not username:
            return jsonify({"success": False, "message": "用户名不能为空"}), 400

        if server.web_password:
            if not password:
                return jsonify({"success": False, "message": "请输入密码"}), 401
            if password != server.web_password:
                return jsonify({"success": False, "message": "密码错误"}), 401

        token = server._generate_login_token(username)

        return jsonify(
            {
                "success": True,
                "message": "登录成功",
                "token": token,
                "expires_days": server.token_expire_days,
            }
        )

    @app.route("/api/verify-token", methods=["POST"])
    def verify_token():
        data = request.json
        token = data.get("token", "").strip()

        if not token:
            return jsonify({"success": False, "message": "Token 不能为空"}), 400

        username = server._validate_login_token(token)

        if username:
            return jsonify(
                {"success": True, "username": username, "message": "Token 验证成功"}
            )
        return jsonify({"success": False, "message": "Token 已失效或无效"}), 401

    @app.route("/api/logout", methods=["POST"])
    def logout():
        data = request.json
        token = data.get("token", "").strip()

        if token and token in server.login_tokens:
            del server.login_tokens[token]
            _log.info(f"[Auth] Token removed: {token[:8]}...")
            server._save_login_tokens()

        return jsonify({"success": True, "message": "已退出登录"})
