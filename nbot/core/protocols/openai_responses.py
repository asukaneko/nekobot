"""
OpenAI Responses Protocol (v1/responses)

Handles the OpenAI Responses API format (/v1/responses), which uses
an "input" array instead of "messages" and returns results in an
"output" array with typed content blocks.
"""

import json
from typing import Any, Callable, Dict, List, Optional

from nbot.core.model_adapter import (
    NormalizedModelResponse,
    normalize_usage_dict,
    repair_mojibake_text,
)
from nbot.core.protocols.base import ModelProtocol
from nbot.core.sampling import is_reasoning_model, resolve_token_limit


class OpenAIResponsesProtocol(ModelProtocol):
    """OpenAI Responses API protocol (/v1/responses)."""

    @property
    def name(self) -> str:
        return "openai_responses"

    @property
    def display_name(self) -> str:
        return "OpenAI Responses API"

    @property
    def url_suffix(self) -> str:
        return "/responses"

    def resolve_url(
        self,
        base_url: str,
        model: str = "",
        *,
        append_base_url_path: bool = True,
        **opts: Any,
    ) -> str:
        url_base = (base_url or "").rstrip("/")
        if not url_base:
            raise ValueError("base_url 未配置")
        if not append_base_url_path:
            return url_base
        if "/responses" in url_base and "/chat/" not in url_base:
            return url_base
        if url_base.endswith("/v1"):
            return f"{url_base}/responses"
        return f"{url_base}/responses"

    def build_headers(self, api_key: str, *, stream: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
            headers["Cache-Control"] = "no-cache"
        return headers

    def build_payload(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
        stream: bool = False,
        max_tokens: int = 4096,
        extra_body: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Build Responses API payload.

        Uses "input" (array of typed items) instead of "messages".
        """
        input_items = self._convert_messages_to_input(messages)

        payload: Dict[str, Any] = {
            "model": model,
            "input": input_items,
        }
        base_url = str(kwargs.get("base_url") or "")
        is_codex_backend = "chatgpt.com/backend-api/codex" in base_url
        if is_codex_backend:
            payload["store"] = False

        if tools:
            payload["tools"] = self._convert_tools(tools)
            if tool_choice:
                mapped = self._map_tool_choice(tool_choice)
                if mapped is not None:
                    payload["tool_choice"] = mapped

        if max_tokens and not is_codex_backend:
            payload["max_output_tokens"] = max_tokens

        if stream:
            payload["stream"] = True

        extra = dict(extra_body) if extra_body else {}
        if extra:
            token_limit = resolve_token_limit(extra)
            if token_limit and not is_codex_backend:
                payload["max_output_tokens"] = token_limit
            if is_reasoning_model(model):
                # 推理系模型只接受默认温度，top_p 同样不生效
                extra.pop("temperature", None)
                extra.pop("top_p", None)
            for key in ("temperature", "top_p", "instructions"):
                if key in extra:
                    payload[key] = extra[key]

        return payload

    def parse_response(
        self,
        data: Dict[str, Any],
        *,
        model: str = "",
        base_url: str = "",
        provider_type: str = "",
        fallback_tool_parser: Optional[Callable] = None,
    ) -> NormalizedModelResponse:
        """Parse Responses API response.

        The response has an "output" array containing typed items:
        - type "message": has "content" array with text blocks
        - type "function_call": has "name", "arguments", "call_id"
        """
        content = ""
        thinking_content = ""
        tool_calls = []
        finish_reason = "stop"

        for item in data.get("output", []):
            item_type = item.get("type", "")

            if item_type == "message":
                for block in item.get("content", []):
                    block_type = block.get("type", "")
                    if block_type == "output_text":
                        text = block.get("text", "")
                        content += repair_mojibake_text(text)
                    elif block_type == "reasoning_text":
                        thinking_content += repair_mojibake_text(block.get("text", ""))

            elif item_type == "function_call":
                arguments = item.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except Exception:
                        arguments = {}
                tool_calls.append({
                    "id": item.get("call_id", ""),
                    "name": item.get("name", ""),
                    "arguments": arguments if isinstance(arguments, dict) else {},
                })

            elif item_type == "reasoning":
                for block in item.get("content", []):
                    thinking_content += repair_mojibake_text(block.get("text", ""))

        if tool_calls:
            finish_reason = "tool_calls"

        usage = normalize_usage_dict(data.get("usage"))

        return NormalizedModelResponse(
            content=content,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            thinking_content=thinking_content,
            usage=usage,
            raw_message={"output": data.get("output", [])},
            raw_data=data,
        )

    def parse_stream_chunk(
        self,
        chunk_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Parse Responses API streaming chunk."""
        event_type = chunk_data.get("type", "")

        if event_type == "response.output_text.delta":
            text = chunk_data.get("delta", "")
            if text:
                text = repair_mojibake_text(text)
                return {"type": "content", "content": text}

        if event_type == "response.output_text.done":
            return None

        if event_type == "response.function_call_arguments.delta":
            return None

        if event_type == "response.completed":
            response = chunk_data.get("response", {})
            usage = normalize_usage_dict(response.get("usage"))
            if usage:
                return {"type": "usage", "usage": usage}
            return {"type": "stop"}

        if event_type == "response.output_item.done":
            item = chunk_data.get("item", {})
            if item.get("type") == "function_call":
                arguments = item.get("arguments", "{}")
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except Exception:
                        arguments = {}
                return {
                    "type": "tool_call_start",
                    "tool_call": {
                        "id": item.get("call_id", ""),
                        "name": item.get("name", ""),
                        "arguments": arguments,
                    },
                }

        return None

    def supports_tools(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages_to_input(
        messages: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI messages to Responses API input format."""
        input_items: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                input_items.append({
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": content}],
                })
                continue

            if role == "user":
                if isinstance(content, list):
                    blocks = []
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                blocks.append({"type": "input_text", "text": item.get("text", "")})
                            elif item.get("type") == "image_url":
                                blocks.append({
                                    "type": "input_image",
                                    "image_url": item.get("image_url", {}).get("url", ""),
                                })
                        elif isinstance(item, str):
                            blocks.append({"type": "input_text", "text": item})
                    input_items.append({
                        "type": "message",
                        "role": "user",
                        "content": blocks,
                    })
                else:
                    input_items.append({
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": str(content)}],
                    })

            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    if content:
                        input_items.append({
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        })
                    for tc in tool_calls:
                        # 兼容扁平格式 {id,name,arguments} 和嵌套格式 {type,function:{name,arguments}}
                        if tc.get("type") == "function":
                            func = tc.get("function", {})
                            tc_name = func.get("name", "")
                            tc_id = tc.get("id", "")
                            args = func.get("arguments", "{}")
                        else:
                            tc_name = tc.get("name", "")
                            tc_id = tc.get("id", "")
                            args = tc.get("arguments", "{}")
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except Exception:
                                args = {}
                        input_items.append({
                            "type": "function_call",
                            "name": tc_name,
                            "arguments": args,
                            "call_id": tc_id,
                        })
                elif content:
                    input_items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                    })

            elif role == "tool":
                input_items.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": content if isinstance(content, str) else str(content),
                })

        return input_items

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert OpenAI-style tools to Responses API format."""
        converted = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                converted.append({
                    "type": "function",
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "parameters": func.get("parameters", {"type": "object", "properties": {}}),
                    "strict": False,
                })
        return converted

    @staticmethod
    def _map_tool_choice(tool_choice: str) -> Optional[Dict[str, Any]]:
        """Map OpenAI tool_choice to Responses API format."""
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "required"}
        if tool_choice == "none":
            return {"type": "none"}
        return {"type": "auto"}
