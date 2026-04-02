from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities, ChannelEnvelope
from nbot.core.session_store import build_qq_session_id


class QQChannelAdapter(BaseChannelAdapter):
    channel_name = "qq"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_stream=False,
            supports_progress_updates=False,
            supports_file_send=True,
            supports_stop=False,
        )

    def build_envelope(self, **kwargs) -> ChannelEnvelope:
        metadata = dict(kwargs.get("metadata") or {})
        user_id = kwargs.get("user_id") or ""
        group_id = metadata.get("group_id")
        group_user_id = metadata.get("group_user_id")
        conversation_id = kwargs.get("conversation_id") or build_qq_session_id(
            user_id=user_id or None,
            group_id=group_id,
            group_user_id=group_user_id,
        )
        sender = kwargs.get("sender") or ("qq_user" if user_id else "qq_group_user")
        return ChannelEnvelope(
            channel=self.channel_name,
            conversation_id=conversation_id,
            user_id=user_id,
            sender=sender,
            attachments=list(kwargs.get("attachments") or []),
            metadata=metadata,
        )

    def build_manager_payload(
        self,
        *,
        role: str,
        content: str,
        sender: str = "",
        conversation_id: str = "",
        attachments=None,
        metadata=None,
        source=None,
        user_id: str = "",
        group_id: str = "",
        group_user_id: str = "",
    ) -> dict:
        is_private = bool(user_id)
        resolved_conversation_id = conversation_id or build_qq_session_id(
            user_id=user_id or None,
            group_id=group_id or None,
            group_user_id=group_user_id or None,
        )
        resolved_sender = sender
        if not resolved_sender:
            if is_private:
                resolved_sender = user_id
            elif role == "user" and group_user_id:
                resolved_sender = group_user_id
        return super().build_manager_payload(
            role=role,
            content=content,
            sender=resolved_sender,
            conversation_id=resolved_conversation_id,
            attachments=attachments,
            metadata=metadata,
            source=source or ("qq_private" if is_private else "qq_group"),
        )
