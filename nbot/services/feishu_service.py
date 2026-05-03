import base64
import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

import requests

from nbot.channels.feishu import FeishuChannelAdapter
from nbot.core.ai_pipeline import (
    AIPipeline,
    PipelineContext,
    PipelineCallbacks,
    PipelineResult,
    handle_tool_confirmation,
)

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"


def resolve_config_secret(
    config: Dict[str, Any],
    value_key: str,
    env_key: str,
    default_env: str = "",
) -> str:
    """从配置或环境变量获取密钥

    支持两种方式配置:
    1. 直接填写值: value_key
    2. 环境变量名: env_key (优先)
    """
    # 首先尝试从环境变量获取
    env_name = str(config.get(env_key) or "").strip()
    if env_name:
        value = os.getenv(env_name)
        if value:
            return value.strip()
    # 如果没有环境变量，尝试直接获取值
    direct_value = str(config.get(value_key) or "").strip()
    if direct_value:
        return direct_value
    # 最后尝试默认环境变量
    if default_env:
        value = os.getenv(default_env)
        if value:
            return value.strip()
    return ""


def resolve_feishu_credentials(config: Dict[str, Any]) -> Dict[str, str]:
    """解析飞书应用凭证

    返回:
        {
            "app_id": "cli_xxx",
            "app_secret": "xxx",
            "encrypt_key": "xxx",  # 可选
            "verification_token": "xxx",  # 可选
        }
    """
    return {
        "app_id": resolve_config_secret(
            config, "app_id", "app_id_env", default_env="FEISHU_APP_ID"
        ),
        "app_secret": resolve_config_secret(
            config, "app_secret", "app_secret_env", default_env="FEISHU_APP_SECRET"
        ),
        "encrypt_key": resolve_config_secret(
            config, "encrypt_key", "encrypt_key_env", default_env="FEISHU_ENCRYPT_KEY"
        ),
        "verification_token": resolve_config_secret(
            config, "verification_token", "verification_token_env", default_env="FEISHU_VERIFICATION_TOKEN"
        ),
    }


def get_tenant_access_token(app_id: str, app_secret: str) -> Optional[str]:
    """获取飞书 tenant_access_token

    企业自建应用使用此接口获取 tenant_access_token
    """
    if not app_id or not app_secret:
        return None

    url = f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal"
    payload = {"app_id": app_id, "app_secret": app_secret}

    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        if data.get("code") == 0:
            return data.get("tenant_access_token")
        return None
    except Exception:
        return None


def send_feishu_message(
    token: str,
    chat_id: str,
    text: str,
    *,
    reply_message_id: Optional[str] = None,
    msg_type: str = "text",
) -> Dict[str, Any]:
    """发送飞书消息

    Args:
        token: tenant_access_token
        chat_id: 会话ID
        text: 消息文本
        reply_message_id: 回复的消息ID
        msg_type: 消息类型，默认text

    Returns:
        API响应结果
    """
    url = f"{FEISHU_API_BASE}/im/v1/messages"

    # 构建消息内容
    if msg_type == "text":
        content = json.dumps({"text": text}, ensure_ascii=False)
    else:
        content = json.dumps({"text": text}, ensure_ascii=False)

    params = {"receive_id_type": "chat_id"}
    payload = {
        "receive_id": chat_id,
        "msg_type": msg_type,
        "content": content,
    }

    if reply_message_id:
        payload["reply_in_thread"] = True
        # 飞书API需要使用reply消息ID作为root_id来回复线程
        payload["root_id"] = reply_message_id

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    response = requests.post(
        url, params=params, json=payload, headers=headers, timeout=30
    )
    response.raise_for_status()
    return response.json()


def verify_feishu_request(
    config: Dict[str, Any],
    signature: str,
    timestamp: str,
    nonce: str,
    body: str,
) -> bool:
    """验证飞书请求签名

    飞书事件订阅请求会携带签名，用于验证请求来源

    Args:
        config: 频道配置
        signature: 请求头中的 X-Lark-Signature
        timestamp: 请求头中的 X-Lark-Request-Timestamp
        nonce: 请求头中的 X-Lark-Request-Nonce
        body: 请求体

    Returns:
        签名是否有效
    """
    credentials = resolve_feishu_credentials(config)
    encrypt_key = credentials.get("encrypt_key")

    if not encrypt_key:
        # 如果没有配置加密密钥，跳过验证
        return True

    # 构建签名字符串: timestamp + nonce + encrypt_key + body
    sign_str = f"{timestamp}{nonce}{encrypt_key}{body}"

    # 计算SHA256哈希
    calculated_signature = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()

    return calculated_signature == signature


def verify_feishu_token(config: Dict[str, Any], token: str) -> bool:
    """验证飞书 verification_token

    飞书事件订阅URL验证时会携带token
    """
    credentials = resolve_feishu_credentials(config)
    expected_token = credentials.get("verification_token")

    if not expected_token:
        # 如果没有配置token，跳过验证
        return True

    return token == expected_token


# ============================================================================
# 飞书 管道回调
# ============================================================================


class FeishuCallbacks(PipelineCallbacks):
    """飞书 Webhook 频道的管道回调实现。"""

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
            "session_id": f"feishu:{self.parsed['chat_id']}",
            "session_type": "feishu",
        }

    def send_response(self, ctx: PipelineContext, message: Dict[str, Any]) -> None:
        send_feishu_message(
            self.token,
            self.parsed["chat_id"],
            message.get("content", ""),
            reply_message_id=self.parsed.get("message_id"),
        )


# ============================================================================
# 入口函数
# ============================================================================


def answer_feishu_event(
    server: Any, channel: Dict[str, Any], event: Dict[str, Any]
) -> Dict[str, Any]:
    """处理飞书事件并回复

    Args:
        server: 服务器实例
        channel: 频道配置
        event: 飞书事件数据

    Returns:
        处理结果
    """
    adapter = FeishuChannelAdapter()
    parsed = adapter.parse_event(event or {})
    if not parsed:
        return {"ok": True, "ignored": True}

    config = channel.get("config") or {}
    credentials = resolve_feishu_credentials(config)

    if not credentials["app_id"] or not credentials["app_secret"]:
        raise ValueError("未配置飞书应用凭证，请设置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")

    # 获取访问令牌
    token = get_tenant_access_token(credentials["app_id"], credentials["app_secret"])
    if not token:
        raise ValueError("获取飞书访问令牌失败，请检查应用凭证")

    # 确认/拒绝待执行命令
    content = handle_tool_confirmation(
        parsed["content"],
        f"feishu:{parsed['chat_id']}",
        log_prefix="Feishu",
    )

    chat_request = adapter.build_chat_request(
        conversation_id=f"feishu:{parsed['chat_id']}",
        user_id=parsed.get("user_id", ""),
        content=content,
        sender=parsed.get("sender", "feishu_user"),
        metadata=parsed.get("metadata", {}),
    )

    ctx = PipelineContext(chat_request=chat_request, adapter=adapter)
    callbacks = FeishuCallbacks(server, token, parsed)

    pipeline = AIPipeline()
    result = pipeline.process(ctx, callbacks)

    return {"ok": True, "result": result.final_content}


def handle_feishu_challenge(config: Dict[str, Any], data: Dict[str, Any]) -> Dict[str, Any]:
    """处理飞书URL验证请求

    飞书在配置事件订阅时，会发送challenge进行URL验证
    """
    adapter = FeishuChannelAdapter()
    challenge = adapter.parse_challenge(data)

    if challenge:
        # 验证token（如果配置了）
        token = data.get("token")
        if token and not verify_feishu_token(config, token):
            raise ValueError("飞书 verification_token 验证失败")

        return {"challenge": challenge}

    return {"ok": False, "error": "无效的challenge请求"}
