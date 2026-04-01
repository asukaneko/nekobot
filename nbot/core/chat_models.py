from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional
import uuid


@dataclass
class ChatRequest:
    channel: str
    conversation_id: str
    user_id: Optional[str] = None
    content: str = ""
    sender: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    parent_message_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def for_web(
        cls,
        session_id: str,
        content: str,
        sender: str,
        attachments: Optional[List[Dict[str, Any]]] = None,
        parent_message_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ChatRequest":
        return cls(
            channel="web",
            conversation_id=session_id,
            content=content,
            sender=sender,
            attachments=list(attachments or []),
            parent_message_id=parent_message_id,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def for_qq(
        cls,
        conversation_id: str,
        content: str,
        *,
        user_id: Optional[str] = None,
        sender: str = "qq_user",
        attachments: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "ChatRequest":
        return cls(
            channel="qq",
            conversation_id=conversation_id,
            user_id=user_id,
            content=content,
            sender=sender,
            attachments=list(attachments or []),
            metadata=dict(metadata or {}),
        )


@dataclass
class ChatResponse:
    final_content: str = ""
    assistant_message: Optional[Dict[str, Any]] = None
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    can_continue: bool = False
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_assistant_message(self, sender: str = "AI") -> Dict[str, Any]:
        if self.assistant_message is not None:
            return self.assistant_message

        content = self.error or self.final_content
        message = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "sender": sender,
        }

        if self.error:
            message["error"] = True
        if self.can_continue:
            message["can_continue"] = True
        if self.tool_trace:
            message["tool_call_history"] = self.tool_trace
        if self.metadata:
            message.update(self.metadata)

        self.assistant_message = message
        return message
