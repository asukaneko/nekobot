from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities, ChannelEnvelope


class WebChannelAdapter(BaseChannelAdapter):
    channel_name = "web"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_stream=True,
            supports_progress_updates=True,
            supports_file_send=True,
            supports_stop=True,
        )

    def build_envelope(self, **kwargs) -> ChannelEnvelope:
        metadata = dict(kwargs.get("metadata") or {})
        metadata.setdefault("channel", self.channel_name)
        sender = kwargs.get("sender") or "web_user"
        return ChannelEnvelope(
            channel=self.channel_name,
            conversation_id=kwargs.get("conversation_id") or "",
            user_id=kwargs.get("user_id") or "",
            sender=sender,
            attachments=list(kwargs.get("attachments") or []),
            metadata=metadata,
        )

    def build_heartbeat_user_message(
        self, conversation_id: str, content: str
    ) -> dict:
        return self.build_message(
            role="user",
            content=f"銆怘eartbeat 浠诲姟銆慭n{content}",
            sender="system",
            conversation_id=conversation_id,
            metadata={
                "source": "heartbeat",
                "is_heartbeat": True,
                "hide_in_web": False,
            },
        )

    def build_heartbeat_assistant_message(
        self, conversation_id: str, content: str
    ) -> dict:
        from nbot.core.chat_models import ChatResponse

        return self.build_assistant_message(
            ChatResponse(final_content=content),
            conversation_id=conversation_id,
            sender="AI",
            metadata={
                "source": "heartbeat",
                "is_heartbeat": True,
                "hide_in_web": False,
            },
        )

    def build_workflow_user_message(
        self, conversation_id: str, content: str, workflow_id: str
    ) -> dict:
        return self.build_message(
            role="user",
            content=content,
            sender="user",
            conversation_id=conversation_id,
            metadata={"workflow_id": workflow_id},
        )

    def build_workflow_assistant_message(
        self, conversation_id: str, content: str, workflow_id: str
    ) -> dict:
        from nbot.core.chat_models import ChatResponse

        return self.build_assistant_message(
            ChatResponse(final_content=content),
            conversation_id=conversation_id,
            sender="AI",
            metadata={"workflow_id": workflow_id},
        )
