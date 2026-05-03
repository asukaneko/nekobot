import os
from typing import Any, Dict, Optional

import requests

from nbot.channels.telegram import TelegramChannelAdapter
from nbot.core.ai_pipeline import (
    AIPipeline,
    PipelineContext,
    PipelineCallbacks,
    PipelineResult,
    handle_tool_confirmation,
)

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


# ============================================================================
# Telegram 管道回调
# ============================================================================


class TelegramCallbacks(PipelineCallbacks):
    """Telegram 频道的管道回调实现。"""

    def __init__(
        self,
        server: Any,
        token: str,
        parsed: Dict[str, Any],
    ):
        self.server = server
        self.token = token
        self.parsed = parsed

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        return str(
            getattr(self.server, "personality", {}).get("systemPrompt") or ""
        ).strip()

    def get_workspace_context(self, ctx: PipelineContext) -> Dict[str, Any]:
        return {
            "session_id": f"telegram:{self.parsed['chat_id']}",
            "session_type": "telegram",
        }

    def send_response(self, ctx: PipelineContext, message: Dict[str, Any]) -> None:
        send_telegram_message(
            self.token,
            self.parsed["chat_id"],
            message.get("content", ""),
            reply_to_message_id=self.parsed.get("message_id"),
        )


# ============================================================================
# 入口函数
# ============================================================================


def answer_telegram_update(
    server: Any, channel: Dict[str, Any], update: Dict[str, Any]
) -> Dict[str, Any]:
    adapter = TelegramChannelAdapter()
    parsed = adapter.parse_update(update or {})
    if not parsed:
        return {"ok": True, "ignored": True}

    config = channel.get("config") or {}
    token = resolve_telegram_token(config)
    if not token:
        raise ValueError("未配置 Telegram bot token，请设置 TELEGRAM_BOT_TOKEN")

    # 确认/拒绝待执行命令
    content = handle_tool_confirmation(
        parsed["content"],
        f"telegram:{parsed['chat_id']}",
        log_prefix="Telegram",
    )

    chat_request = adapter.build_chat_request(
        conversation_id=f"telegram:{parsed['chat_id']}",
        user_id=parsed.get("user_id", ""),
        content=content,
        sender=parsed.get("sender", "telegram_user"),
        metadata=parsed.get("metadata", {}),
    )

    ctx = PipelineContext(chat_request=chat_request, adapter=adapter)
    callbacks = TelegramCallbacks(server, token, parsed)

    pipeline = AIPipeline()
    result = pipeline.process(ctx, callbacks)

    # 返回飞书格式兼容的结果（调用方期望 Dict）
    return {"ok": True, "result": result.final_content}
