from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import uuid

if TYPE_CHECKING:
    from nbot.core.chat_models import ChatRequest, ChatResponse


@dataclass
class ChannelCapabilities:
    supports_stream: bool = False
    supports_progress_updates: bool = False
    supports_file_send: bool = False
    supports_stop: bool = False


@dataclass
class ChannelEnvelope:
    channel: str
    conversation_id: str
    user_id: str = ""
    sender: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class BaseChannelAdapter:
    channel_name: str = "unknown"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities()

    def build_envelope(self, **kwargs) -> ChannelEnvelope:
        return ChannelEnvelope(channel=self.channel_name, **kwargs)

    def build_chat_request(
        self,
        *,
        conversation_id: Optional[str] = None,
        content: str,
        sender: str = "",
        user_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        parent_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ChatRequest":
        from nbot.core.chat_models import ChatRequest

        normalized_content = self.normalize_inbound_message(content)
        normalized_attachments = self.normalize_attachments(attachments)
        envelope = self.build_envelope(
            conversation_id=conversation_id or "",
            user_id=str(user_id) if user_id is not None else "",
            sender=sender,
            attachments=normalized_attachments,
            metadata=dict(metadata or {}),
        )
        return ChatRequest(
            channel=envelope.channel,
            conversation_id=envelope.conversation_id,
            user_id=envelope.user_id or None,
            content=normalized_content,
            sender=envelope.sender,
            attachments=envelope.attachments,
            parent_message_id=parent_message_id,
            metadata=envelope.metadata,
        )

    def build_message(
        self,
        *,
        role: str,
        content: str,
        sender: str = "",
        conversation_id: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        message = {
            "id": str(uuid.uuid4()),
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "sender": sender,
            "source": source or self.channel_name,
        }
        if conversation_id:
            message["session_id"] = conversation_id
        if attachments:
            message["attachments"] = list(attachments)
        if metadata:
            message.update(metadata)
        return message

    def build_manager_payload(
        self,
        *,
        role: str,
        content: str,
        sender: str = "",
        conversation_id: str = "",
        attachments: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        source: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload = {
            "role": role,
            "content": content,
            "sender": sender,
            "source": source or self.channel_name,
            "session_id": conversation_id,
        }
        if attachments:
            payload["attachments"] = list(attachments)
        if metadata:
            payload["metadata"] = dict(metadata)
        return payload

    def build_manager_payload_from_message(
        self,
        message: Dict[str, Any],
        *,
        default_role: str,
        default_content: str,
        default_sender: str = "",
        default_conversation_id: str = "",
        default_source: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        merged_metadata = dict(message.get("metadata") or {})
        if metadata:
            merged_metadata.update(metadata)
        return self.build_manager_payload(
            role=message.get("role", default_role),
            content=message.get("content", default_content),
            sender=message.get("sender", default_sender),
            conversation_id=message.get("session_id", default_conversation_id),
            attachments=message.get("attachments"),
            metadata=merged_metadata,
            source=message.get("source", default_source or self.channel_name),
            **extra,
        )

    def build_assistant_message(
        self,
        chat_response: "ChatResponse",
        *,
        conversation_id: str,
        sender: str = "AI",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        message = chat_response.to_assistant_message(sender=sender)
        message_metadata = dict(metadata or {})
        for key in ("can_continue", "tool_call_history", "error"):
            if key in message:
                message_metadata[key] = message[key]

        channel_message = self.build_message(
            role=message.get("role", "assistant"),
            content=message.get("content", ""),
            sender=message.get("sender", sender),
            conversation_id=conversation_id,
            metadata=message_metadata,
        )
        channel_message["id"] = message.get("id", channel_message["id"])
        channel_message["timestamp"] = message.get(
            "timestamp", channel_message["timestamp"]
        )
        return channel_message

    def normalize_inbound_message(self, content: str) -> str:
        return (content or "").strip()

    def normalize_attachments(
        self, attachments: Optional[List[Dict[str, Any]]]
    ) -> List[Dict[str, Any]]:
        return list(attachments or [])
