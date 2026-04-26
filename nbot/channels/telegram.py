from typing import Any, Dict, Optional

from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities, ChannelEnvelope


class TelegramChannelAdapter(BaseChannelAdapter):
    channel_name = "telegram"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_stream=False,
            supports_progress_updates=False,
            supports_file_send=False,
            supports_stop=False,
        )

    def build_envelope(self, **kwargs) -> ChannelEnvelope:
        metadata = dict(kwargs.get("metadata") or {})
        chat_id = metadata.get("telegram_chat_id") or metadata.get("chat_id")
        conversation_id = kwargs.get("conversation_id") or (
            f"telegram:{chat_id}" if chat_id else ""
        )
        return ChannelEnvelope(
            channel=self.channel_name,
            conversation_id=conversation_id,
            user_id=kwargs.get("user_id") or "",
            sender=kwargs.get("sender") or "telegram_user",
            attachments=list(kwargs.get("attachments") or []),
            metadata=metadata,
        )

    def parse_update(self, update: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return None

        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return None

        text = message.get("text") or message.get("caption") or ""
        text = self.normalize_inbound_message(text)
        if not text:
            return None

        username = sender.get("username") or sender.get("first_name") or "telegram_user"
        user_id = sender.get("id")
        return {
            "chat_id": str(chat_id),
            "message_id": message.get("message_id"),
            "user_id": str(user_id) if user_id is not None else "",
            "sender": username,
            "content": text,
            "metadata": {
                "telegram_update_id": update.get("update_id"),
                "telegram_chat_id": str(chat_id),
                "telegram_message_id": message.get("message_id"),
                "telegram_chat_type": chat.get("type"),
            },
        }
