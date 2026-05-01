"""
Anthropic Messages API 适配器
将OpenAI格式的请求转换为Anthropic Messages API格式
"""
import json
from typing import Dict, List, Any, Optional


def convert_openai_messages_to_anthropic(messages: List[Dict[str, Any]]) -> tuple:
    """
    将OpenAI格式的消息列表转换为Anthropic格式
    
    Anthropic格式:
    - system: 单独的字符串（可选）
    - messages: 消息列表，每条消息有 role 和 content
    
    Returns:
        (system_message, anthropic_messages)
    """
    system_message = ""
    anthropic_messages = []
    
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if role == "system":
            # Anthropic将system消息作为单独的参数
            system_message = content if isinstance(content, str) else ""
        elif role == "user":
            anthropic_messages.append({
                "role": "user",
                "content": content if isinstance(content, str) else str(content)
            })
        elif role == "assistant":
            # 处理可能包含tool_calls的assistant消息
            anthropic_content = content if isinstance(content, str) else ""
            
            # 如果有tool_calls，需要特殊处理
            if "tool_calls" in msg and msg["tool_calls"]:
                content_blocks = []
                if anthropic_content:
                    content_blocks.append({"type": "text", "text": anthropic_content})
                
                for tc in msg["tool_calls"]:
                    if tc.get("type") == "function":
                        func = tc.get("function", {})
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc.get("id", ""),
                            "name": func.get("name", ""),
                            "input": json.loads(func.get("arguments", "{}")) if isinstance(func.get("arguments"), str) else func.get("arguments", {})
                        })
                
                if content_blocks:
                    anthropic_messages.append({
                        "role": "assistant",
                        "content": content_blocks
                    })
                else:
                    anthropic_messages.append({
                        "role": "assistant",
                        "content": anthropic_content
                    })
            else:
                anthropic_messages.append({
                    "role": "assistant",
                    "content": anthropic_content
                })
        elif role == "tool":
            # OpenAI的tool结果转换为Anthropic的tool_result
            anthropic_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else str(content)
                }]
            })
    
    return system_message, anthropic_messages


def convert_openai_tools_to_anthropic(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    将OpenAI格式的tools转换为Anthropic的tools格式
    """
    anthropic_tools = []
    
    for tool in tools:
        if tool.get("type") == "function":
            func = tool.get("function", {})
            anthropic_tools.append({
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}})
            })
    
    return anthropic_tools


def build_anthropic_payload(
    model: str,
    messages: List[Dict[str, Any]],
    max_tokens: int = 4096,
    temperature: Optional[float] = None,
    top_p: Optional[float] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    stream: bool = False
) -> Dict[str, Any]:
    """
    构建Anthropic Messages API的请求payload
    """
    system_message, anthropic_messages = convert_openai_messages_to_anthropic(messages)
    
    payload = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": max_tokens
    }
    
    if system_message:
        payload["system"] = system_message
    
    if temperature is not None:
        payload["temperature"] = temperature
    
    if top_p is not None:
        payload["top_p"] = top_p
    
    if tools:
        payload["tools"] = convert_openai_tools_to_anthropic(tools)
    
    if stream:
        payload["stream"] = True
    
    return payload


def parse_anthropic_response(response_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析Anthropic Messages API的响应，转换为OpenAI格式
    """
    content = ""
    thinking_content = ""
    tool_calls = []
    
    # 解析content数组
    for block in response_data.get("content", []):
        block_type = block.get("type", "")
        
        if block_type == "text":
            content += block.get("text", "")
        elif block_type == "thinking":
            thinking_content += block.get("thinking", "")
        elif block_type == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}))
                }
            })
    
    result = {
        "content": content,
        "thinking_content": thinking_content,
        "tool_calls": tool_calls if tool_calls else None,
        "finish_reason": "stop" if response_data.get("stop_reason") in ["end_turn", "stop"] else "tool_calls" if tool_calls else "stop",
        "usage": {
            "prompt_tokens": response_data.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": response_data.get("usage", {}).get("output_tokens", 0),
            "total_tokens": (
                response_data.get("usage", {}).get("input_tokens", 0) + 
                response_data.get("usage", {}).get("output_tokens", 0)
            )
        }
    }
    
    return result


def parse_anthropic_stream_chunk(chunk_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    解析Anthropic Messages API的流式响应chunk
    """
    chunk_type = chunk_data.get("type", "")
    
    if chunk_type == "content_block_delta":
        delta = chunk_data.get("delta", {})
        delta_type = delta.get("type", "")
        
        if delta_type == "text_delta":
            return {
                "type": "content",
                "content": delta.get("text", "")
            }
        elif delta_type == "thinking_delta":
            return {
                "type": "thinking",
                "thinking": delta.get("thinking", "")
            }
    
    elif chunk_type == "content_block_start":
        block = chunk_data.get("content_block", {})
        block_type = block.get("type", "")
        
        if block_type == "tool_use":
            return {
                "type": "tool_call_start",
                "tool_call": {
                    "id": block.get("id", ""),
                    "name": block.get("name", ""),
                    "arguments": block.get("input", {})
                }
            }
    
    elif chunk_type == "message_stop":
        return {
            "type": "stop"
        }
    
    return None


def get_anthropic_headers(api_key: str) -> Dict[str, str]:
    """
    获取Anthropic API的请求头
    """
    return {
        "x-api-key": api_key,
        "Content-Type": "application/json",
        "anthropic-version": "2023-06-01"
    }
