import json
import logging
import os
from datetime import datetime

from flask import jsonify, request, send_from_directory

from nbot.web.push_keys import ensure_vapid_keys

try:
    from pywebpush import WebPushException, webpush
except ImportError:  # pragma: no cover - surfaced through API response at runtime
    WebPushException = Exception
    webpush = None

_log = logging.getLogger(__name__)


def _push_store_path(server):
    return os.path.join(server.data_dir, "push", "subscriptions.json")


def _load_subscriptions(server):
    path = _push_store_path(server)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as exc:
        _log.warning("Failed to load push subscriptions: %s", exc)
        return []


def _save_subscriptions(server, subscriptions):
    path = _push_store_path(server)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(subscriptions, f, ensure_ascii=False, indent=2)


def _get_vapid(server):
    return ensure_vapid_keys(server.data_dir)


def _is_session_visible(server, session_id):
    if not session_id:
        return False
    visible_sessions = getattr(server, "visible_web_sessions", {})
    return any(value == session_id for value in visible_sessions.values())


def send_web_push(
    server,
    title,
    body,
    url="/",
    session_id=None,
    tag="nekobot-message",
    skip_visible=True,
):
    if webpush is None:
        return {"sent": 0, "failed": 0, "error": "pywebpush is not installed"}

    if skip_visible and _is_session_visible(server, session_id):
        return {"sent": 0, "failed": 0, "skipped": "session_visible"}

    vapid = _get_vapid(server)
    payload = json.dumps(
        {
            "title": title or "NekoBot",
            "body": body or "You have a new message.",
            "url": url or "/",
            "tag": tag or "nekobot-message",
        },
        ensure_ascii=False,
    )

    kept = []
    sent = 0
    failed = 0

    for item in _load_subscriptions(server):
        if session_id and item.get("session_id") not in ("", session_id):
            kept.append(item)
            continue

        subscription = item.get("subscription")
        if not subscription:
            continue

        try:
            webpush(
                subscription_info=subscription,
                data=payload,
                vapid_private_key=vapid["private_key_path"],
                vapid_claims={"sub": vapid["subject"]},
                ttl=60 * 60,
            )
            item["last_sent_at"] = datetime.now().isoformat()
            kept.append(item)
            sent += 1
        except WebPushException as exc:
            failed += 1
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code not in (404, 410):
                kept.append(item)
            _log.warning("Web Push failed: %s", exc)
        except Exception as exc:
            failed += 1
            kept.append(item)
            _log.warning("Web Push failed: %s", exc)

    _save_subscriptions(server, kept)
    return {"sent": sent, "failed": failed}


def register_push_routes(app, server):
    @app.route("/sw.js")
    def service_worker():
        return send_from_directory(
            server.static_folder,
            "sw.js",
            mimetype="application/javascript",
        )

    @app.route("/manifest.webmanifest")
    def web_manifest():
        return send_from_directory(
            server.static_folder,
            "manifest.webmanifest",
            mimetype="application/manifest+json",
        )

    @app.get("/api/push/public-key")
    def push_public_key():
        vapid = _get_vapid(server)
        return jsonify({"publicKey": vapid["public_key"]})

    @app.get("/api/push/status")
    def push_status():
        return jsonify(
            {
                "supported": webpush is not None,
                "subscriptions": len(_load_subscriptions(server)),
                "publicKeyConfigured": bool(_get_vapid(server).get("public_key")),
            }
        )

    @app.post("/api/push/subscribe")
    def push_subscribe():
        data = request.get_json(force=True) or {}
        subscription = data.get("subscription")
        session_id = str(data.get("session_id") or "")

        if not subscription or not subscription.get("endpoint"):
            return jsonify({"ok": False, "error": "invalid subscription"}), 400

        endpoint = subscription["endpoint"]
        subscriptions = [
            item
            for item in _load_subscriptions(server)
            if item.get("subscription", {}).get("endpoint") != endpoint
        ]
        subscriptions.append(
            {
                "session_id": session_id,
                "subscription": subscription,
                "created_at": datetime.now().isoformat(),
                "user_agent": request.headers.get("User-Agent", ""),
            }
        )
        _save_subscriptions(server, subscriptions)
        return jsonify({"ok": True})

    @app.post("/api/push/unsubscribe")
    def push_unsubscribe():
        data = request.get_json(force=True) or {}
        endpoint = data.get("endpoint")
        if not endpoint:
            return jsonify({"ok": False, "error": "endpoint required"}), 400

        subscriptions = [
            item
            for item in _load_subscriptions(server)
            if item.get("subscription", {}).get("endpoint") != endpoint
        ]
        _save_subscriptions(server, subscriptions)
        return jsonify({"ok": True})

    @app.post("/api/push/test")
    def push_test():
        data = request.get_json(silent=True) or {}
        session_id = str(data.get("session_id") or "")
        result = send_web_push(
            server,
            title="NekoBot test notification",
            body=data.get("body") or "Web Push is working.",
            url=f"/?session_id={session_id}" if session_id else "/",
            session_id=session_id or None,
            tag="nekobot-test",
            skip_visible=False,
        )
        return jsonify({"ok": "error" not in result, "result": result})
