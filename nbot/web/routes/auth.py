import logging
import os

from flask import jsonify, request

_log = logging.getLogger(__name__)


def register_auth_routes(app, server):
    def _get_request_token() -> str:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()

        header_token = (
            request.headers.get("X-Auth-Token", "").strip()
            or request.headers.get("X-Token", "").strip()
        )
        if header_token:
            return header_token

        cookie_token = request.cookies.get("nbot_auth_token", "").strip()
        if cookie_token:
            return cookie_token

        payload = request.get_json(silent=True) or {}
        return str(payload.get("token", "")).strip()

    @app.route("/")
    @app.route("/dashboard")
    def index():
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "templates", "index.html"
        )
        with open(template_path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/html"}

    @app.route("/api/login", methods=["POST"])
    def login():
        data = request.get_json(silent=True) or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", "")).strip()

        if not username:
            return jsonify({"success": False, "message": "Username is required"}), 400

        if not server.web_password:
            return jsonify(
                {
                    "success": False,
                    "message": "Web password is not configured",
                }
            ), 503

        if not password:
            return jsonify({"success": False, "message": "Password is required"}), 401

        # 登录失败限流检查
        client_ip = request.remote_addr or "unknown"
        wait_seconds = server._check_login_rate_limit(client_ip)
        if wait_seconds is not None:
            _log.warning(f"[Auth] 登录限流: IP={client_ip}, 需等待 {wait_seconds}s")
            return jsonify(
                {
                    "success": False,
                    "message": f"Too many failed attempts, please try again in {wait_seconds} seconds",
                }
            ), 429

        # 密码验证（支持明文和 bcrypt 哈希）
        if not server._verify_password(password):
            server._record_login_failure(client_ip)
            return jsonify({"success": False, "message": "Invalid password"}), 401

        # 登录成功，清除失败记录
        server._reset_login_failures(client_ip)

        token = server._generate_login_token(username)
        response = jsonify(
            {
                "success": True,
                "message": "Login successful",
                "token": token,
                "expires_days": server.token_expire_days,
            }
        )
        response.set_cookie(
            "nbot_auth_token",
            token,
            max_age=server.token_expire_days * 24 * 60 * 60,
            httponly=True,
            samesite="Lax",
            secure=request.is_secure or os.getenv("NBOT_SECURE_COOKIES") == "1",
        )
        return response

    @app.route("/api/verify-token", methods=["POST"])
    def verify_token():
        token = _get_request_token()
        if not token:
            return jsonify({"success": False, "message": "Token is required"}), 400

        username = server._validate_login_token(token)
        if username:
            return jsonify(
                {"success": True, "username": username, "message": "Token is valid"}
            )
        return jsonify({"success": False, "message": "Invalid or expired token"}), 401

    @app.route("/api/logout", methods=["POST"])
    def logout():
        token = _get_request_token()
        if token:
            token_hash = server._hash_token(token)
            if token_hash in server.login_tokens:
                del server.login_tokens[token_hash]
                _log.info(f"[Auth] Token removed: {token_hash[:16]}...")
                server._save_login_tokens()

        response = jsonify({"success": True, "message": "Logged out"})
        response.delete_cookie("nbot_auth_token")
        return response
