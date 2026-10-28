"""
Gemini Native Protocol (generateContent)

Handles the Google Gemini native API format, converting between
OpenAI-style internal representation and Gemini's contents/parts format.

Key differences from OpenAI:
- assistant role -> "model"
- content -> parts[]
- system -> system_instruction (top level)
- choices -> candidates
- tool_calls -> functionCall parts
- tool results -> functionResponse parts (role: "user")
- Auth via API key in URL query parameter
"""

import json
import uuid
from typing import Any, Callable, Dict, List, Optional

from nbot.core.model_adapter import (
    NormalizedModelResponse,
    repair_mojibake_text,
)
from nbot.core.protocols.base import ModelProtocol
from nbot.core.sampling import normalize_temperature, normalize_top_p, resolve_token_limit

# Gemini API 不支持的 JSON Schema 扩展字段
# additionalProperties 会被 Gemini 拒绝：
# "Unknown name \"additionalProperties\" at 'tools[0].function_declarations[*]...': Cannot find field"
_JSON_SCHEMA_EXTRA_KEYS = frozenset({
    "$schema", "$id", "$ref", "$defs", "definitions",
    "additionalProperties",
})


def _strip_json_schema_extras(obj: Any) -> Any:
    """递归移除 Gemini API 不接受的 JSON Schema 扩展字段。"""
    if isinstance(obj, dict):
        return {
            k: _strip_json_schema_extras(v)
            for k, v in obj.items()
            if k not in _JSON_SCHEMA_EXTRA_KEYS
        }
    if isinstance(obj, list):
        return [_strip_json_schema_extras(item) for item in obj]
    return obj


class GeminiNativeProtocol(ModelProtocol):
    """Gemini native generateContent protocol."""

    @property
    def name(self) -> str:
        return "gemini_native"

    @property
    def display_name(self) -> str:
        return "Gemini Native (generateContent)"

    @property
    def url_suffix(self) -> str:
        return ":generateContent"

    def resolve_url(
        self,
        base_url: str,
        model: str = "",
        *,
        append_base_url_path: bool = True,
        stream: bool = False,
        api_key: str = "",
        **opts: Any,
    ) -> str:
        url_base = (base_url or "").rstrip("/")
        if not url_base:
            raise ValueError("base_url 未配置")

        if ":generateContent" in url_base or ":streamGenerateContent" in url_base:
            url = url_base
        elif "streamGenerateContent" in url_base:
            url = url_base
        else:
            action = "streamGenerateContent" if stream else "generateContent"
            if "/models/" in url_base:
                url = f"{url_base}:{action}"
            elif not append_base_url_path:
                url = url_base
            else:
                url = f"{url_base}/models/{model}:{action}"

        # 流式请求统一加 alt=sse，让 Gemini 返回 SSE 格式（data: ... 前缀）
        # 否则默认返回 JSON 数组流，难以用 SSE 解析器处理
        if stream and "streamGenerateContent" in url and "alt=sse" not in url:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}alt=sse"

        # Google 官方 API 需要 ?key=xxx 认证
        if api_key and ("googleapis.com" in url or "google.com" in url):
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}key={api_key}"

        return url

    def build_headers(self, api_key: str, *, stream: bool = False) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if stream:
            headers["Accept"] = "text/event-stream"
        # 代理/中转服务通常通过 Authorization: Bearer 传递令牌
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
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
        system_instruction, contents = self._convert_messages(messages)

        payload: Dict[str, Any] = {"contents": contents}

        if system_instruction:
            payload["systemInstruction"] = system_instruction

        extra = dict(extra_body) if extra_body else {}
        gen_config: Dict[str, Any] = {}
        token_limit = resolve_token_limit(extra, max_tokens)
        if token_limit:
            gen_config["maxOutputTokens"] = token_limit
        # 采样参数：Gemini 使用驼峰 temperature / topP / topK
        temperature = normalize_temperature(extra.get("temperature"))
        if temperature is not None:
            gen_config["temperature"] = temperature
        top_p = normalize_top_p(extra.get("top_p", extra.get("topP")))
        if top_p is not None:
            gen_config["topP"] = top_p
        for key in ("topK", "candidateCount", "responseMimeType", "responseSchema"):
            if key in extra:
                gen_config[key] = extra[key]
        top_k = extra.get("top_k")
        if "topK" not in gen_config and isinstance(top_k, int) and not isinstance(top_k, bool):
            if top_k > 0:
                gen_config["topK"] = top_k
        # 停止字符串：Gemini 协议使用 generationConfig.stopSequences
        stops = extra.get("stop") or extra.get("stopSequences")
        if stops:
            gen_config["stopSequences"] = stops
        if gen_config:
            payload["generationConfig"] = gen_config

        if tools:
            payload["tools"] = self._convert_tools(tools)

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
        finish_reason = "stop"

        candidates = data.get("candidates", [])
        if not candidates:
            prompt_feedback = data.get("promptFeedback", {})
            block_reason = prompt_feedback.get("blockReason", "")
            if block_reason:
                return NormalizedModelResponse(
                    content=f"[Gemini 安全拦截: {block_reason}]",
                    finish_reason="stop",
                    usage=self._parse_usage(data),
                    raw_data=data,
                )
            return NormalizedModelResponse(
                content="",
                finish_reason="stop",
                usage=self._parse_usage(data),
                raw_data=data,
            )

        candidate = candidates[0]
        gemini_finish = candidate.get("finishReason", "STOP")
        if gemini_finish == "STOP":
            finish_reason = "stop"
        elif gemini_finish == "MAX_TOKENS":
            finish_reason = "length"
        else:
            finish_reason = "stop"

        parts = candidate.get("content", {}).get("parts", [])
        for part in parts:
            if "text" in part:
                content += part["text"]
            if "thought" in part:
                thinking_content += part.get("thought", "")
            if "functionCall" in part:
                fc = part["functionCall"]
                raw_args = fc.get("args", {})
                tool_call = {
                    "id": fc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": fc.get("name", ""),
                    "arguments": raw_args if isinstance(raw_args, dict) else {},
                }
                # 保留 thoughtSignature，后续请求必须原样回传
                # thoughtSignature 可能在 functionCall 内部或 part 级别
                sig = fc.get("thoughtSignature") or part.get("thoughtSignature")
                if sig:
                    tool_call["_thought_signature"] = sig
                tool_calls.append(tool_call)

        content = repair_mojibake_text(content)
        thinking_content = repair_mojibake_text(thinking_content)

        if tool_calls:
            finish_reason = "tool_calls"

        return NormalizedModelResponse(
            content=content,
            finish_reason=finish_reason,
            tool_calls=tool_calls,
            thinking_content=thinking_content,
            usage=self._parse_usage(data),
            raw_data=data,
        )

    def parse_stream_chunk(
        self,
        chunk_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        candidates = chunk_data.get("candidates") or []
        if not candidates:
            # 可能是只含 usageMetadata 的尾部 chunk
            usage = self._parse_usage(chunk_data)
            if usage:
                return {"type": "usage", "usage": usage}
            return None

        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        # content 字段可能为 None（流结束 chunk 只含 finishReason）
        content_obj = candidate.get("content") or {}
        parts = content_obj.get("parts") or [] if isinstance(content_obj, dict) else []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "text" in part:
                text = repair_mojibake_text(part.get("text", ""))
                if text:
                    return {"type": "content", "content": text}
            if "functionCall" in part:
                fc = part.get("functionCall") or {}
                raw_args = fc.get("args", {})
                tool_call = {
                    "id": fc.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                    "name": fc.get("name", ""),
                    "arguments": raw_args if isinstance(raw_args, dict) else {},
                }
                sig = fc.get("thoughtSignature") or part.get("thoughtSignature")
                if sig:
                    tool_call["_thought_signature"] = sig
                return {
                    "type": "tool_call_start",
                    "tool_call": tool_call,
                }

        usage = self._parse_usage(chunk_data)
        if usage:
            return {"type": "usage", "usage": usage}

        return None

    def supports_tools(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_usage(data: Dict[str, Any]) -> Dict[str, int]:
        meta = data.get("usageMetadata", {})
        if not meta:
            return {}
        return {
            "prompt_tokens": meta.get("promptTokenCount", 0),
            "completion_tokens": meta.get("candidatesTokenCount", 0),
            "total_tokens": meta.get("totalTokenCount", 0),
        }

    @staticmethod
    def _convert_messages(
        messages: List[Dict[str, Any]],
    ) -> tuple:
        """Convert OpenAI-style messages to Gemini contents/parts format.

        Returns (system_instruction, contents).
        """
        system_parts: List[Dict[str, str]] = []
        contents: List[Dict[str, Any]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                if isinstance(content, str) and content:
                    system_parts.append({"text": content})
                continue

            if role == "user":
                parts = []
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                parts.append({"text": item.get("text", "")})
                            elif item.get("type") == "image_url":
                                url = item.get("image_url", {}).get("url", "")
                                if url.startswith("data:"):
                                    mime, _, b64 = url.partition(",")
                                    mime = mime.split(":")[1].split(";")[0]
                                    parts.append({
                                        "inlineData": {
                                            "mimeType": mime,
                                            "data": b64,
                                        }
                                    })
                        elif isinstance(item, str):
                            parts.append({"text": item})
                elif isinstance(content, str):
                    parts.append({"text": content})
                if parts:
                    contents.append({"role": "user", "parts": parts})
                continue

            if role == "assistant":
                parts = []
                thinking = msg.get("_thinking_content", "")
                if thinking:
                    parts.append({"thought": thinking})
                if isinstance(content, str) and content:
                    parts.append({"text": content})
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                parts.append({"text": block.get("text", "")})
                            elif block.get("type") == "thinking":
                                parts.append({"thought": block.get("thinking", "")})
                tool_calls = msg.get("tool_calls", [])
                for tc in tool_calls:
                    func = tc.get("function", {})
                    name = func.get("name") or tc.get("name", "")
                    args_raw = func.get("arguments") or tc.get("arguments", "{}")
                    if isinstance(args_raw, str):
                        try:
                            args = json.loads(args_raw)
                        except Exception:
                            args = {}
                    else:
                        args = args_raw
                    fc_part: Dict[str, Any] = {
                        "functionCall": {
                            "name": name,
                            "args": args,
                        }
                    }
                    call_id = tc.get("id")
                    if call_id:
                        fc_part["functionCall"]["id"] = call_id
                    # 保留 thoughtSignature，Gemini API 要求后续请求原样回传
                    sig = tc.get("_thought_signature")
                    if sig:
                        fc_part["thoughtSignature"] = sig
                    parts.append(fc_part)
                if parts:
                    contents.append({"role": "model", "parts": parts})
                continue

            if role == "tool":
                result_content = content if isinstance(content, str) else str(content)
                try:
                    response_data = json.loads(result_content)
                except Exception:
                    response_data = {"result": result_content}

                # 从历史中找对应的 functionCall name
                func_name = msg.get("name", "tool_result")
                for prev_msg in reversed(contents):
                    if prev_msg.get("role") == "model":
                        for p in prev_msg.get("parts", []):
                            if "functionCall" in p:
                                func_name = p["functionCall"].get("name", func_name)
                                break

                contents.append({
                    "role": "user",
                    "parts": [{
                        "functionResponse": {
                            "id": msg.get("tool_call_id", ""),
                            "name": func_name,
                            "response": response_data,
                        }
                    }]
                })
                continue

        system_instruction = None
        if system_parts:
            system_instruction = {"parts": system_parts}

        return system_instruction, contents

    @staticmethod
    def _convert_tools(
        tools: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """Convert OpenAI-style tools to Gemini function_declarations format."""
        declarations = []
        for tool in tools:
            if tool.get("type") == "function":
                func = tool.get("function", {})
                parameters = func.get("parameters", {"type": "object", "properties": {}})
                # Gemini API 不接受 JSON Schema 扩展字段，需要清除
                parameters = _strip_json_schema_extras(parameters)
                declarations.append({
                    "name": func.get("name", ""),
                    "description": func.get("description", ""),
                    "parameters": parameters,
                })
        return [{"functionDeclarations": declarations}]
