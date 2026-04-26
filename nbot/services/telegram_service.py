import os
from typing import Any, Dict, Optional

import requests

from nbot.channels.telegram import TelegramChannelAdapter

TELEGRAM_API_BASE = "https://api.telegram.org"


def resolve_config_secret(
    config: Dict[str, Any],
    value_key: str,
    env_key: str,
    default_env: str = "",
) -> str:
    env_name = str(config.get(env_key) or default_env or "").strip()
    if env_name:
        value = os.getenv(env_name)
        if value:
            return value.strip()
    return str(config.get(value_key) or "").strip()


def resolve_telegram_token(config: Dict[str, Any]) -> str:
    return resolve_config_secret(
        config,
        "bot_token",
        "bot_token_env",
        default_env="TELEGRAM_BOT_TOKEN",
    )


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    reply_to_message_id: Optional[int] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096] if text else "",
        "disable_web_page_preview": True,
    }
    if reply_to_message_id:
        payload["reply_to_message_id"] = reply_to_message_id

    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{token}/sendMessage",
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def set_telegram_webhook(config: Dict[str, Any], webhook_url: str) -> Dict[str, Any]:
    token = resolve_telegram_token(config)
    if not token:
        raise ValueError("未配置 Telegram bot token，请设置 TELEGRAM_BOT_TOKEN")

    payload: Dict[str, Any] = {"url": webhook_url}
    secret = resolve_config_secret(config, "secret_token", "secret_token_env")
    if (config.get("secret_token") or config.get("secret_token_env")) and not secret:
        raise ValueError("Telegram webhook secret 未配置或环境变量为空")
    if secret:
        payload["secret_token"] = secret

    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{token}/setWebhook",
        json=payload,
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _extract_ai_text(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "").strip()
    except Exception:
        return ""


def _build_messages(server: Any, parsed: Dict[str, Any]) -> list:
    messages = []
    system_prompt = str(getattr(server, "personality", {}).get("prompt") or "").strip()
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": parsed["content"]})
    return messages


def answer_telegram_update(server: Any, channel: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    adapter = TelegramChannelAdapter()
    parsed = adapter.parse_update(update or {})
    if not parsed:
        return {"ok": True, "ignored": True}

    config = channel.get("config") or {}
    token = resolve_telegram_token(config)
    if not token:
        raise ValueError("未配置 Telegram bot token，请设置 TELEGRAM_BOT_TOKEN")

    try:
        from nbot.services.ai import ai_client, refresh_runtime_ai_config

        refresh_runtime_ai_config()
        response = ai_client.chat_completion(_build_messages(server, parsed), stream=False)
        reply_text = _extract_ai_text(response) or "我暂时没有生成可发送的回复。"
    except Exception as exc:
        reply_text = f"处理消息失败：{exc}"

    return send_telegram_message(
        token,
        parsed["chat_id"],
        reply_text,
        reply_to_message_id=parsed.get("message_id"),
    )
