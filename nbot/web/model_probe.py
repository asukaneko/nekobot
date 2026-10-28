"""模型能力实测（Probe）

用于替代"看文档猜能力"：对一句极短请求实测 chat / stream / tools / vision，
把结果写回模型配置（``supports_tools`` / ``supports_stream`` / ``supports_vision``
与 ``probed_at``），前端只展示不自动改行为。

仅支持 OpenAI 兼容协议（覆盖绝大多数自建/中转端点）；Anthropic/Gemini 原生协议
返回 ``supported=False``，避免把厂商特有的探测逻辑做成半成品。
"""

import base64
import logging
import struct
import time
import zlib
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)


def _build_tiny_png() -> bytes:
    """生成 1x1 透明 PNG（vision 探测用，体积最小）。"""
    raw = b"\x00" + b"\x00\x00\x00\x00"  # filter byte + RGBA
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)

    def chunk(chunk_type: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", crc)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


_TINY_PNG = base64.b64encode(_build_tiny_png()).decode("ascii")

PROBE_TOOL: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "probe_echo",
            "description": "回显输入内容。当用户要求调用工具时必须调用它。",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string", "description": "要回显的文本"}},
                "required": ["text"],
            },
        },
    }
]

SUPPORTED_PROTOCOLS = ("openai_chat", "openai_compatible", "openai")


def _post(url: str, headers: Dict[str, str], payload: Dict[str, Any], timeout: float):
    import requests

    return requests.post(url, headers=headers, json=payload, timeout=timeout)


def _extract_message(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], dict) else {}
        message = first.get("message")
        if isinstance(message, dict):
            return message
        delta = first.get("delta")
        if isinstance(delta, dict):
            return delta
    return {}


def probe_model_config(
    config: Dict[str, Any],
    *,
    timeout: float = 20.0,
    checks: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """实测模型能力。

    Args:
        config: 模型配置（需要 base_url / model / api_key / provider_type 等）
        timeout: 单次请求超时（秒）
        checks: 要测的项，默认 ``["chat", "tools", "vision"]``

    Returns:
        ``{"supported": bool, "ok": bool, "error": str, "latency_ms": int,
           "capabilities": {"chat","tools","vision","stream"}, "details": {...}}``
    """
    if not isinstance(config, dict):
        return {"supported": False, "ok": False, "error": "配置格式有误"}

    provider_type = str(config.get("provider_type") or "openai_compatible").lower()
    if provider_type not in SUPPORTED_PROTOCOLS:
        return {
            "supported": False,
            "ok": False,
            "error": f"暂不支持实测该协议（{provider_type}），仅支持 OpenAI 兼容协议",
            "capabilities": {},
            "details": {},
        }

    base_url = str(config.get("base_url") or "").rstrip("/")
    model_name = str(config.get("model") or "")
    api_key = str(config.get("api_key") or "")
    if not base_url or not model_name:
        return {
            "supported": False,
            "ok": False,
            "error": "缺少 base_url 或 model",
            "capabilities": {},
            "details": {},
        }

    from nbot.core.protocols import get_protocol

    protocol = get_protocol(provider_type)
    try:
        url = protocol.resolve_url(
            base_url,
            model=model_name,
            append_base_url_path=config.get("append_base_url_path", True),
            api_key=api_key,
        )
    except Exception as exc:
        return {
            "supported": False,
            "ok": False,
            "error": f"URL 解析失败: {exc}",
            "capabilities": {},
            "details": {},
        }
    headers = protocol.build_headers(api_key, stream=False)

    wanted = set(checks or ["chat", "tools", "vision", "stream"])
    details: Dict[str, Dict[str, Any]] = {}
    capabilities: Dict[str, bool] = {}
    started = time.perf_counter()

    # 推理系模型会先消耗思考 token，太小的上限会得到空回复，探测预算直接放开
    from nbot.core.sampling import is_reasoning_model

    reasoning_model = is_reasoning_model(model_name)

    def _probe_budget(default: int):
        return None if reasoning_model else default

    def _base_messages(text: str) -> List[Dict[str, Any]]:
        return [{"role": "user", "content": text}]

    def _run(name: str, payload_builder, verdict) -> None:
        try:
            payload = payload_builder()
            response = _post(url, headers, payload, timeout)
            status = int(getattr(response, "status_code", 0) or 0)
            data = {}
            try:
                data = response.json()
            except Exception:
                data = {}
            ok = status == 200
            detail = {
                "ok": ok,
                "status": status,
                "error": "" if ok else _short_error(response, data),
            }
            if ok:
                try:
                    detail["verdict"] = bool(verdict(data))
                except Exception:
                    detail["verdict"] = False
                capabilities[name] = detail["verdict"]
            else:
                capabilities[name] = False
            details[name] = detail
        except Exception as exc:
            details[name] = {"ok": False, "status": 0, "error": str(exc)[:200], "verdict": False}
            capabilities[name] = False

    def _build_chat(stream: bool = False) -> Dict[str, Any]:
        return protocol.build_payload(
            model_name,
            _base_messages("ping"),
            stream=stream,
            max_tokens=_probe_budget(16),
            base_url=base_url,
            provider_type=provider_type,
        )

    if "chat" in wanted:
        _run("chat", lambda: _build_chat(False), lambda data: bool(_extract_message(data)))

    if "tools" in wanted:
        def _build_tools() -> Dict[str, Any]:
            return protocol.build_payload(
                model_name,
                _base_messages("请调用 probe_echo 工具回显 hello"),
                tools=PROBE_TOOL,
                tool_choice="auto",
                stream=False,
                max_tokens=_probe_budget(64),
                base_url=base_url,
                provider_type=provider_type,
            )

        def _tools_verdict(data: Any) -> bool:
            message = _extract_message(data)
            if message.get("tool_calls"):
                return True
            choices = data.get("choices") if isinstance(data, dict) else None
            if isinstance(choices, list) and choices:
                finish = str((choices[0] or {}).get("finish_reason") or "")
                if finish in {"tool_calls", "function_call"}:
                    return True
            return False

        _run("tools", _build_tools, _tools_verdict)

    if "vision" in wanted:
        def _build_vision() -> Dict[str, Any]:
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "这张图是什么颜色？只回答颜色。"},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_TINY_PNG}"},
                        },
                    ],
                }
            ]
            return protocol.build_payload(
                model_name,
                messages,
                stream=False,
                max_tokens=_probe_budget(16),
                base_url=base_url,
                provider_type=provider_type,
            )

        _run("vision", _build_vision, lambda data: bool(_extract_message(data)))

    if "stream" in wanted:
        try:
            payload = _build_chat(True)
            response = _post(url, headers, payload, timeout)
            ok = int(getattr(response, "status_code", 0) or 0) == 200
            capabilities["stream"] = ok
            details["stream"] = {
                "ok": ok,
                "status": int(getattr(response, "status_code", 0) or 0),
                "error": "" if ok else _short_error(response, {}),
                "verdict": ok,
            }
        except Exception as exc:
            capabilities["stream"] = False
            details["stream"] = {"ok": False, "status": 0, "error": str(exc)[:200], "verdict": False}

    latency_ms = int((time.perf_counter() - started) * 1000)
    chat_ok = bool(details.get("chat", {}).get("ok")) if "chat" in details else True
    first_error = next(
        (item.get("error") for item in details.values() if item.get("error")),
        "",
    )
    return {
        "supported": True,
        "ok": chat_ok,
        "error": "" if chat_ok else (first_error or "chat 探测失败"),
        "latency_ms": latency_ms,
        "capabilities": capabilities,
        "details": details,
    }


def _short_error(response, data: Any) -> str:
    try:
        if isinstance(data, dict):
            error = data.get("error")
            if isinstance(error, dict):
                text = error.get("message") or error.get("code")
                if text:
                    return str(text)[:200]
            if isinstance(error, str):
                return error[:200]
        text = getattr(response, "text", "") or ""
        return str(text)[:200]
    except Exception:
        return ""


def apply_probe_result(config: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    """把探测结果写回配置（原地修改并返回）。"""
    if not isinstance(config, dict) or not isinstance(result, dict):
        return config
    if not result.get("supported"):
        return config
    caps = result.get("capabilities") or {}
    for key in ("tools", "stream", "vision"):
        if key in caps:
            config[f"supports_{key}"] = bool(caps[key])
    config["capabilities_probed"] = True
    config["capabilities_probed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    config["capabilities_probe_latency_ms"] = result.get("latency_ms", 0)
    return config
