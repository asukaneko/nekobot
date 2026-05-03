from typing import Any, Dict, Optional

from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities, ChannelEnvelope


class FeishuChannelAdapter(BaseChannelAdapter):
    """飞书频道适配器"""

    channel_name = "feishu"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_stream=False,
            supports_progress_updates=False,
            supports_file_send=False,
            supports_stop=False,
        )

    def build_envelope(self, **kwargs) -> ChannelEnvelope:
        metadata = dict(kwargs.get("metadata") or {})
        chat_id = metadata.get("chat_id") or metadata.get("open_chat_id")
        conversation_id = kwargs.get("conversation_id") or (
            f"feishu:{chat_id}" if chat_id else ""
        )
        return ChannelEnvelope(
            channel=self.channel_name,
            conversation_id=conversation_id,
            user_id=kwargs.get("user_id") or "",
            sender=kwargs.get("sender") or "feishu_user",
            attachments=list(kwargs.get("attachments") or []),
            metadata=metadata,
        )

    def parse_event(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """解析飞书事件回调数据

        支持的消息类型:
        - im.message.receive_v1: 接收消息事件 (text / image)
        """
        header = event.get("header") or {}
        event_type = header.get("event_type")

        if event_type != "im.message.receive_v1":
            return None

        event_data = event.get("event") or {}
        message = event_data.get("message") or {}
        sender = event_data.get("sender") or {}
        sender_info = sender.get("sender_id") or {}

        chat_type = message.get("chat_type")
        chat_id = message.get("chat_id")
        message_type = message.get("message_type")
        content = message.get("content", "{}")

        if not chat_id:
            return None

        # 解析消息内容
        text = ""
        attachments = []
        if isinstance(content, str):
            try:
                import json
                content_obj = json.loads(content)
            except Exception:
                content_obj = {}
        elif isinstance(content, dict):
            content_obj = content
        else:
            content_obj = {}

        if message_type == "image":
            # 图片消息：提取 image_key 和 message_id
            image_key = content_obj.get("image_key", "")
            msg_id = message.get("message_id", "")
            if image_key:
                attachments.append({
                    "type": "image",
                    "source": "feishu",
                    "image_key": image_key,
                    "message_id": msg_id,
                })
        else:
            # 文本消息
            text = content_obj.get("text", "")

        text = self.normalize_inbound_message(text)
        if not text and not attachments:
            return None

        # 获取发送者信息
        sender_id = sender_info.get("open_id") or sender_info.get("user_id") or ""
        sender_name = "feishu_user"

        return {
            "chat_id": str(chat_id),
            "chat_type": chat_type,
            "message_id": message.get("message_id"),
            "user_id": str(sender_id),
            "sender": sender_name,
            "content": text,
            "message_type": message_type,
            "attachments": attachments,
            "metadata": {
                "feishu_event_type": event_type,
                "feishu_chat_id": str(chat_id),
                "feishu_chat_type": chat_type,
                "feishu_message_id": message.get("message_id"),
                "feishu_sender_id": sender_id,
                "feishu_message_type": message_type,
            },
        }

    def parse_challenge(self, data: Dict[str, Any]) -> Optional[str]:
        """解析飞书URL验证请求

        飞书在配置事件订阅时，会发送challenge进行URL验证
        """
        challenge = data.get("challenge")
        if challenge:
            return str(challenge)
        return None
