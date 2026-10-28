"""
Anthropic Messages Protocol (/v1/messages)

Handles the Anthropic Messages API format, converting between
OpenAI-style internal representation and Anthropic's native format.
"""

import json
from typing import Any, Callable, Dict, List, Optional

from nbot.core.model_adapter import (
    NormalizedModelResponse,
    repair_mojibake_text,
)
from nbot.core.protocols.base import ModelProtocol
from nbot.core.sampling import normalize_temperature, normalize_top_p, resolve_token_limit

DEFAULT_MAX_TOKENS = 4096


class AnthropicMessagesProtocol(ModelProtocol):
    """Anthropic Messages API protocol (/v1/messages)."""

    @property
    def name(self) -> str:
        return "anthropic_messages"

    @property
    def display_name(self) -> str:
        return "Anthropic Messages"

    @property
    def url_suffix(self) -> str:
        return "/v1/messages"

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
        if "/v1/messages" in url_base:
            return url_base
        return f"{url_base}/v1/messages"

    def build_headers(self, api_key: str, *, stream: bool = False) -> Dict[str, str]:
        headers = {
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
        return headers

    def build_payload(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        stream: bool = False,
        max_tokens: int = 4096,
        extra_body: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        system_message, anthropic_messages = self._convert_messages(messages)

        extra = dict(extra_body) if extra_body else {}
        # Anthropic 的 max_tokens 是必填项，未配置时沿用兜底值
        token_limit = resolve_token_limit(extra, max_tokens) or DEFAULT_MAX_TOKENS

        payload: Dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": token_limit,
        }

        # prompt caching：system 作为 content block 标记 ephemeral 断点
        if system_message:
            if extra.get("prompt_caching"):
                payload["system"] = [{
                    "type": "text",
                    "text": system_message,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                payload["system"] = system_message

        if tools:
            payload["tools"] = self._convert_tools(tools)
            # 工具定义（稳定前缀）标记缓存断点
            if extra.get("prompt_caching") and payload["tools"]:
                payload["tools"][-1] = {
                    **payload["tools"][-1],
                    "cache_control": {"type": "ephemeral"},
                }
            if tool_choice:
                mapped = self._map_tool_choice(tool_choice)
                if mapped is not None:
                    payload["tool_choice"] = mapped

        if stream:
            payload["stream"] = True

        # 采样参数：Anthropic 的温度上限是 1，超出会被接口拒绝
        temperature = normalize_temperature(extra.get("temperature"))
        if temperature is not None:
            payload["temperature"] = min(temperature, 1.0)
        top_p = normalize_top_p(extra.get("top_p"))
        if top_p is not None:
            payload["top_p"] = top_p
        top_k = extra.get("top_k")
        if isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0:
            payload["top_k"] = top_k
        # 停止字符串：Anthropic 协议使用 stop_sequences
        if extra.get("stop"):
            payload["stop_sequences"] = extra["stop"]

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
        content = ""
        thinking_content = ""
        tool_calls = []

        for block in data.get("content", []):
            block_type = block.get("type", "")
            if block_type == "text":
                content += block.get("text", "")
            elif block_type == "thinking":
                thinking_content += block.get("thinking", "")
            elif block_type == "tool_use":
                raw_input = block.get("input", {})
                tool_calls.append({
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": raw_input if isinstance(raw_input, dict) else {},
                })

        content = repair_mojibake_text(content)
        thinking_content = repair_mojibake_text(thinking_content)

        usage_raw = data.get("usage", {})
        usage = {
            "prompt_tokens": usage_raw.get("input_tokens", 0),
            "completion_tokens": usage_raw.get("output_tokens", 0),
            "total_tokens": (
                usage_raw.get("input_tokens", 0)
                + usage_raw.get("output_tokens", 0)
            ),
        }

        stop_reason = data.get("stop_reason", "")
        if stop_reason in ("end_turn", "stop"):
            finish_reason = "stop"
        elif tool_calls:
            finish_reason = "tool_calls"
        else:
            finish_reason = "stop"

        return NormalizedModelResponse(
            content=content,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            thinking_content=thinking_content,
            usage=usage,
            raw_message=data,
            raw_data=data,
        )

    def parse_stream_chunk(
        self,
        chunk_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        chunk_type = chunk_data.get("type", "")

        if chunk_type == "content_block_delta":
            delta = chunk_data.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                text = repair_mojibake_text(delta.get("text", ""))
                if text:
                    return {"type": "content", "content": text}
            elif delta_type == "thinking_delta":
                thinking = delta.get("thinking", "")
                if thinking:
                    return {"type": "thinking", "thinking": thinking}

        elif chunk_type == "content_block_start":
            block = chunk_data.get("content_block", {})
            if block.get("type") == "tool_use":
                return {
                    "type": "tool_call_start",
                    "tool_call": {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "arguments": block.get("input", {}),
                    },
                }

        elif chunk_type == "message_stop":
            return {"type": "stop"}

        return None

    def supports_tools(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: List[Dict[str, Any]],
    ) -> tuple:
        """Convert OpenAI-style messages to Anthropic format."""
        system_message = ""
        anthropic_messages: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_message = content if isinstance(content, str) else ""
            elif role == "user":
                anthropic_messages.append({
                    "role": "user",
                    "content": content if isinstance(content, str) else str(content),
                })
            elif role == "assistant":
                anthropic_content = content if isinstance(content, str) else ""
                tool_calls = msg.get("tool_calls")
                # 从 _thinking_content 重建 thinking block（DeepSeek 等要求回传）
                thinking_text = msg.get("_thinking_content", "")

                if tool_calls:
                    blocks = []
                    if thinking_text:
                        blocks.append({"type": "thinking", "thinking": thinking_text})
                    if anthropic_content:
                        blocks.append({"type": "text", "text": anthropic_content})
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
                        blocks.append({
                            "type": "tool_use",
                            "id": tc_id,
                            "name": tc_name,
                            "input": args,
                        })
                    if blocks:
                        anthropic_messages.append({"role": "assistant", "content": blocks})
                    else:
                        anthropic_messages.append({"role": "assistant", "content": anthropic_content})
                else:
                    anthropic_messages.append({"role": "assistant", "content": anthropic_content})

            elif role == "tool":
                # Anthropic 要求所有 tool_result 合并到一条 user 消息中
                tool_result_block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else str(content),
                }
                # 如果前一条消息也是 tool results，合并到同一条
                if (anthropic_messages
                        and anthropic_messages[-1].get("role") == "user"
                        and isinstance(anthropic_messages[-1].get("content"), list)
                        and anthropic_messages[-1]["content"]
                        and anthropic_messages[-1]["content"][0].get("type") == "tool_result"):
                    anthropic_messages[-1]["content"].append(tool_result_block)
                else:
                    anthropic_messages.append({
                        "role": "user",
                        "content": [tool_result_block],
                    })

        return system_message, anthropic_messages

    @staticmethod
    def _convert_tools(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert OpenAI-style tools to Anthropic format."""
        anthropic_tools = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                anthropic_tools.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
                })
        return anthropic_tools

    @staticmethod
    def _map_tool_choice(tool_choice: str) -> Optional[Dict[str, str]]:
        """Map OpenAI tool_choice to Anthropic format."""
        if tool_choice == "auto":
            return {"type": "auto"}
        if tool_choice == "required":
            return {"type": "any"}
        if tool_choice == "none":
            return None
        return {"type": "auto"}
