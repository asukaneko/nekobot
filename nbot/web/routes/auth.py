import logging
import os
import secrets

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
        if not secrets.compare_digest(password, str(server.web_password)):
            return jsonify({"success": False, "message": "Invalid password"}), 401

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
        if token and token in server.login_tokens:
            del server.login_tokens[token]
            _log.info(f"[Auth] Token removed: {token[:8]}...")
            server._save_login_tokens()

        response = jsonify({"success": True, "message": "Logged out"})
        response.delete_cookie("nbot_auth_token")
        return response
