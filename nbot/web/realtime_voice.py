"""
P2-13 Realtime 实时语音（WebSocket 代理）

浏览器 <-> 本服务（Socket.IO 命名空间 /realtime-voice）<-> 厂商 Realtime WebSocket。

支持的厂商（可插拔 registry）：
  - qwen_omni        通义 Qwen-Omni Realtime（DashScope，默认）
  - glm_realtime     智谱 GLM-Realtime
  - openai_realtime  OpenAI Realtime
  - custom_realtime  任意 OpenAI-Realtime 兼容端点（base_url 自填）

协议约定：
  - 与厂商之间走 OpenAI Realtime 风格 JSON 事件（Qwen-Omni / GLM / OpenAI 一致）；
  - 浏览器侧统一用 Socket.IO 事件，收发 base64 PCM16 音频：
      realtime.start     {provider, model, voice, instructions, sample_rate, session_id}
      realtime.audio     {audio: base64 PCM16}
      realtime.commit    {}                    手动提交一段话（server_vad 时可不用）
      realtime.interrupt {}                    打断当前播报
      realtime.stop      {}                    结束会话
    服务端事件：
      ready / session {type, ...} / audio {delta} /
      user_transcript {text} / assistant_transcript_delta {text} /
      assistant_transcript_done {text} / speech_started / speech_stopped /
      response_started / response_done {usage,status} / interrupted /
      reconnecting {attempt,delay} / error {message} / closed {reason}

鉴权：与 Web 面板一致，复用 server._validate_login_token（Bearer / cookie / auth.token）。
配置来源：ai_models.json 中 purpose=realtime_voice|realtime 的模型，其次环境变量，
再次 data/web/settings.json 的 realtime_voice 段。
"""

import base64
import json
import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import quote, urlparse

import websocket
from flask import request

_log = logging.getLogger(__name__)

# Socket.IO 命名空间
REALTIME_NAMESPACE = "/realtime-voice"

# 重连策略
MAX_RECONNECT_ATTEMPTS = 5
RECONNECT_BASE_DELAY = 1.5
RECONNECT_MAX_DELAY = 15.0

# 出站音频队列上限（chunk 数），溢出丢最旧的
MAX_OUTBOUND_CHUNKS = 400
# 单个音频 chunk 的 base64 长度上限（约 512KB PCM）
MAX_AUDIO_CHUNK_CHARS = 700_000
# 单会话最长存活时间（秒），防止浏览器标签页挂着不关
SESSION_MAX_SECONDS = 3600
# 厂商连接建立超时（秒）
CONNECT_TIMEOUT = 15
# 接收轮询超时（秒）
RECV_TIMEOUT = 0.5

DEFAULT_INSTRUCTIONS = (
    "你是一个中文语音助手。回答简短自然，口语化，不要使用 markdown、列表或表情符号；"
    "遇到听不清的内容就礼貌地请对方重复。"
)

DEFAULT_SYSTEM_PROMPT = DEFAULT_INSTRUCTIONS


@dataclass(frozen=True)
class RealtimeProvider:
    """一个 Realtime 厂商的静态描述。"""

    id: str
    label: str
    default_model: str
    default_voice: str
    url: str
    env_keys: tuple[str, ...] = ()
    voice_options: tuple[str, ...] = ()
    extra_headers: tuple[tuple[str, str], ...] = ()
    docs: str = ""
    sample_rate: int = 24000
    protocol: str = "openai_realtime"

    def build_url(self, model: str, base_url: str = "") -> str:
        """拼接 wss 地址（model 作为查询参数）。"""
        base = (base_url or self.url or "").strip().rstrip("/")
        if not base:
            raise RealtimeConfigError(f"{self.id} 未配置 base_url")
        if base.startswith("https://"):
            base = "wss://" + base[len("https://"):]
        elif base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        if not base.startswith(("ws://", "wss://")):
            raise RealtimeConfigError(f"{self.id} 的地址必须是 ws:// 或 wss://")
        if not model:
            return base
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}model={quote(model, safe='')}"

    def auth_headers(self, api_key: str) -> list[str]:
        """WebSocket 握手请求头。"""
        headers = [f"Authorization: Bearer {api_key}"] if api_key else []
        headers.extend(f"{key}: {value}" for key, value in self.extra_headers)
        return headers

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "default_model": self.default_model,
            "default_voice": self.default_voice,
            "voice_options": list(self.voice_options),
            "sample_rate": self.sample_rate,
            "protocol": self.protocol,
            "docs": self.docs,
            "env_keys": list(self.env_keys),
        }


REALTIME_PROVIDERS: dict[str, RealtimeProvider] = {
    "qwen_omni": RealtimeProvider(
        id="qwen_omni",
        label="通义 Qwen-Omni Realtime",
        default_model="qwen3-omni-flash-realtime",
        default_voice="Cherry",
        url="wss://dashscope.aliyuncs.com/api-ws/v1/realtime",
        env_keys=("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
        voice_options=("Cherry", "Serena", "Ethan", "Chelsie"),
        docs="https://help.aliyun.com/zh/model-studio/realtime",
    ),
    "glm_realtime": RealtimeProvider(
        id="glm_realtime",
        label="智谱 GLM-Realtime",
        default_model="glm-realtime-flash",
        default_voice="tongtong",
        url="wss://open.bigmodel.cn/api/paas/v4/realtime",
        env_keys=("ZHIPU_API_KEY", "GLM_API_KEY"),
        voice_options=("tongtong", "chuichui", "xiaochen"),
    ),
    "openai_realtime": RealtimeProvider(
        id="openai_realtime",
        label="OpenAI Realtime",
        default_model="gpt-4o-realtime-preview",
        default_voice="alloy",
        url="wss://api.openai.com/v1/realtime",
        env_keys=("OPENAI_API_KEY",),
        voice_options=("alloy", "echo", "shimmer", "verse", "coral"),
        extra_headers=(("OpenAI-Beta", "realtime=v1"),),
    ),
    "custom_realtime": RealtimeProvider(
        id="custom_realtime",
        label="自定义（OpenAI Realtime 兼容）",
        default_model="",
        default_voice="alloy",
        url="",
        env_keys=("NBOT_REALTIME_API_KEY",),
        voice_options=(),
    ),
}

# 自动选择顺序
_PROVIDER_ORDER = ("qwen_omni", "glm_realtime", "openai_realtime", "custom_realtime")


class RealtimeConfigError(RuntimeError):
    """Realtime 配置缺失或不合法。"""


def _project_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _settings_path() -> str:
    return os.path.join(_project_root(), "data", "web", "settings.json")


def _load_settings_section() -> dict:
    """读取 settings.json 的 realtime_voice 段。"""
    try:
        with open(_settings_path(), encoding="utf-8") as handle:
            settings = json.load(handle)
        section = settings.get("realtime_voice")
        return section if isinstance(section, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        _log.debug("[Realtime] 读取 settings.json 失败: %s", exc)
        return {}


def _resolve_runtime_key(raw_key: str, provider_type: str = "") -> str:
    """解密/解析模型配置里的 api_key（与图片生成同源逻辑）。"""
    if not raw_key:
        return ""
    try:
        from nbot.web.utils.config_loader import resolve_runtime_api_key

        return resolve_runtime_api_key(raw_key, provider_type) or ""
    except Exception:  # noqa: BLE001 - 解析失败就用原值
        return raw_key


def _find_realtime_model(provider_id: str) -> dict:
    """在 ai_models.json / server.ai_models 中找 realtime 用途的模型配置。"""
    models: list[dict] = []
    try:
        from nbot.web.server import WebChatServer

        server = WebChatServer.get_instance()
        if server is not None:
            models = list(getattr(server, "ai_models", []) or [])
    except Exception:  # noqa: BLE001 - 无 server 时回退读文件
        models = []

    if not models:
        try:
            from nbot.web.utils.config_loader import _load_ai_models_from_file

            models = list(_load_ai_models_from_file() or [])
        except Exception as exc:  # noqa: BLE001
            _log.debug("[Realtime] 读取 ai_models 失败: %s", exc)
            return {}

    candidates = [
        model
        for model in models
        if str(model.get("purpose") or "").lower() in ("realtime_voice", "realtime")
        and model.get("enabled", True)
    ]
    if not candidates:
        return {}
    for model in candidates:
        provider_name = str(
            model.get("realtime_provider") or model.get("provider") or ""
        ).strip().lower()
        if provider_name == provider_id:
            return model
    # 没有精确匹配时用第一个（单厂商用户常见）
    return candidates[0]


def _env_api_key(provider: RealtimeProvider) -> tuple[str, str]:
    """按 provider 的环境变量取 key，返回 (key, 变量名)。"""
    for name in provider.env_keys:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, name
    return "", ""


def resolve_realtime_config(
    provider_id: str = "",
    model: str = "",
    voice: str = "",
    base_url: str = "",
    instructions: str = "",
    sample_rate: int | str = 0,
    *,
    require_key: bool = True,
) -> dict:
    """解析一次实时语音会话的完整配置。

    Raises:
        RealtimeConfigError: provider 未知 / 缺 key / 缺 base_url。
    """
    section = _load_settings_section()
    provider_id = (provider_id or section.get("provider") or "").strip().lower()
    if not provider_id:
        provider_id = _default_provider_id()

    provider = REALTIME_PROVIDERS.get(provider_id)
    if provider is None:
        raise RealtimeConfigError(f"未知的实时语音 provider：{provider_id}")

    model_cfg = _find_realtime_model(provider_id)
    model = (model or section.get("model") or model_cfg.get("model") or
             provider.default_model).strip()
    voice = (voice or section.get("voice") or provider.default_voice).strip()
    instructions = (instructions or section.get("instructions") or DEFAULT_INSTRUCTIONS).strip()
    base_url = (base_url or section.get("base_url") or model_cfg.get("base_url") or "").strip()
    api_key = _resolve_runtime_key(
        str(model_cfg.get("api_key") or ""), str(model_cfg.get("provider_type") or "")
    )
    key_source = "ai_models" if api_key else ""
    if not api_key:
        api_key, env_name = _env_api_key(provider)
        key_source = env_name if api_key else ""

    if require_key and not api_key:
        raise RealtimeConfigError(
            f"{provider.label} 未配置 API Key（在 AI 模型中添加 realtime_voice 模型，"
            f"或设置环境变量 {' / '.join(provider.env_keys) or 'API Key'}）"
        )
    if provider.id == "custom_realtime" and not base_url:
        raise RealtimeConfigError("custom_realtime 需要在请求里传 base_url")

    try:
        sample_rate_int = int(sample_rate or section.get("sample_rate") or provider.sample_rate)
    except (TypeError, ValueError):
        sample_rate_int = provider.sample_rate

    return {
        "provider": provider.id,
        "provider_label": provider.label,
        "model": model,
        "voice": voice,
        "instructions": instructions,
        "sample_rate": max(8000, min(48000, sample_rate_int)),
        "url": provider.build_url(model, base_url),
        "headers": provider.auth_headers(api_key),
        "api_key": api_key,
        "key_source": key_source,
        "protocol": provider.protocol,
        "proxy_url": str(model_cfg.get("proxy_url") or section.get("proxy_url") or ""),
    }


def _default_provider_id() -> str:
    """选默认 provider：优先已配好 key 的，否则 qwen_omni。"""
    for provider_id in _PROVIDER_ORDER:
        provider = REALTIME_PROVIDERS[provider_id]
        if _env_api_key(provider)[0]:
            return provider_id
    for provider_id in _PROVIDER_ORDER:
        if _find_realtime_model(provider_id):
            return provider_id
    return "qwen_omni"


def list_realtime_providers() -> list[dict]:
    """provider 列表 + 是否已配置（供前端下拉）。"""
    default_id = _default_provider_id()
    items = []
    for provider_id in _PROVIDER_ORDER:
        provider = REALTIME_PROVIDERS[provider_id]
        env_key, env_name = _env_api_key(provider)
        model_cfg = _find_realtime_model(provider_id)
        configured = bool(env_key or _resolve_runtime_key(str(model_cfg.get("api_key") or "")))
        item = provider.as_dict()
        item.update({
            "configured": configured,
            "key_source": env_name or ("ai_models" if model_cfg else ""),
            "default": provider_id == default_id,
            "model_from_config": str(model_cfg.get("model") or ""),
        })
        items.append(item)
    return items


# ----------------------------------------------------------------------------
# 会话：厂商 WebSocket <-> 浏览器 Socket.IO
# ----------------------------------------------------------------------------


@dataclass
class RealtimeSessionStats:
    """会话统计（给状态接口用）。"""

    session_id: str = ""
    provider: str = ""
    model: str = ""
    reconnects: int = 0
    chunks_in: int = 0
    chunks_out: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    started_at: float = 0.0
    state: str = "idle"


class RealtimeVoiceSession:
    """一次实时语音会话。

    Args:
        emit: 回调 `emit(event: str, payload: dict)`，把事件推给浏览器。
        config: resolve_realtime_config() 的结果。
        session_id: 业务会话 id（可选，仅用于打点）。
    """

    def __init__(self, emit, config: dict, session_id: str = ""):
        self._emit = emit
        self.config = config
        self.session_id = session_id
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._outbound: deque[dict] = deque()
        self._response_active = False
        self._last_ping = 0.0
        self._dropped_chunks = 0
        self.stats = RealtimeSessionStats(
            session_id=session_id,
            provider=config.get("provider", ""),
            model=config.get("model", ""),
            started_at=time.time(),
        )

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> None:
        """启动后台线程，建立与厂商的连接。"""
        if self._thread is not None:
            return
        self.stats.state = "connecting"
        self._thread = threading.Thread(
            target=self._run,
            name=f"realtime-voice-{self.stats.provider}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, reason: str = "client_stop") -> None:
        """主动结束会话。"""
        self._stop.set()
        self.stats.state = "closed"
        with self._lock:
            self._outbound.clear()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - 关闭失败无所谓
                pass
        self._emit("closed", {"reason": reason, "stats": self._stats_payload()})

    @property
    def alive(self) -> bool:
        return not self._stop.is_set()

    def _stats_payload(self) -> dict:
        stats = self.stats
        return {
            "session_id": stats.session_id,
            "provider": stats.provider,
            "model": stats.model,
            "state": stats.state,
            "reconnects": stats.reconnects,
            "chunks_in": stats.chunks_in,
            "chunks_out": stats.chunks_out,
            "bytes_in": stats.bytes_in,
            "bytes_out": stats.bytes_out,
            "duration_ms": int((time.time() - stats.started_at) * 1000) if stats.started_at else 0,
        }

    # -- 浏览器 -> 厂商 ---------------------------------------------------

    def send_audio(self, audio_b64: str) -> bool:
        """把浏览器的一段 base64 PCM16 音频排进发送队列。"""
        audio_b64 = (audio_b64 or "").strip()
        if not audio_b64:
            return False
        if len(audio_b64) > MAX_AUDIO_CHUNK_CHARS:
            _log.warning("[Realtime] 音频块过大，丢弃：%d 字符", len(audio_b64))
            return False
        try:
            raw = base64.b64decode(audio_b64, validate=True)
        except Exception:  # noqa: BLE001 - 非法 base64 直接拒绝
            _log.warning("[Realtime] 收到非法 base64 音频块")
            return False
        if len(raw) % 2 != 0:
            # PCM16 必须是偶数字节，头部残缺时截掉最后 1 字节
            raw = raw[:-1]
            audio_b64 = base64.b64encode(raw).decode("ascii")
        with self._lock:
            if len(self._outbound) >= MAX_OUTBOUND_CHUNKS:
                self._outbound.popleft()
                self._dropped_chunks += 1
            self._outbound.append({
                "type": "input_audio_buffer.append",
                "audio": audio_b64,
            })
            self.stats.chunks_in += 1
            self.stats.bytes_in += len(raw)
        return True

    def commit(self) -> None:
        """手动提交当前输入缓冲。"""
        with self._lock:
            self._outbound.append({"type": "input_audio_buffer.commit"})

    def interrupt(self) -> None:
        """打断：取消厂商当前回复，丢掉还没发出去的输入音频。"""
        with self._lock:
            self._outbound.clear()
            if self._response_active:
                self._outbound.append({"type": "response.cancel"})
        self._response_active = False
        self._emit("interrupted", {"by": "client"})

    # -- 线程主循环 -------------------------------------------------------

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._connect_and_pump()
            except Exception as exc:  # noqa: BLE001 - 任何异常都走重连
                _log.warning("[Realtime] 厂商连接异常: %s", exc)
                if self._stop.is_set():
                    break
                self._emit("error", {"message": str(exc), "retryable": True})
            if self._stop.is_set():
                break
            attempt += 1
            self.stats.reconnects = attempt
            if attempt > MAX_RECONNECT_ATTEMPTS:
                self.stats.state = "failed"
                self._emit("error", {
                    "message": f"实时语音连接中断，已重试 {MAX_RECONNECT_ATTEMPTS} 次",
                    "retryable": False,
                })
                break
            delay = min(RECONNECT_BASE_DELAY * (2 ** (attempt - 1)), RECONNECT_MAX_DELAY)
            self.stats.state = "reconnecting"
            self._emit("reconnecting", {"attempt": attempt, "delay": delay})
            self._stop.wait(delay)

        if self.stats.state != "failed":
            self.stats.state = "closed"

    def _connect_and_pump(self) -> None:
        """建连、发 session.update，然后循环收事件 / 发音频。"""
        kwargs = {"timeout": CONNECT_TIMEOUT}
        kwargs.update(self._proxy_kwargs())
        ws = websocket.create_connection(
            self.config["url"], header=self.config.get("headers") or [], **kwargs
        )
        self._ws = ws
        ws.settimeout(RECV_TIMEOUT)
        try:
            self._send_session_update(ws)
            self.stats.state = "ready"
            self._emit("ready", {
                "provider": self.config.get("provider", ""),
                "label": self.config.get("provider_label", ""),
                "model": self.config.get("model", ""),
                "voice": self.config.get("voice", ""),
                "sample_rate": self.config.get("sample_rate", 24000),
                "reconnects": self.stats.reconnects,
            })
            deadline = time.time() + SESSION_MAX_SECONDS
            while not self._stop.is_set() and time.time() < deadline:
                self._drain_outbound(ws)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    self._maybe_ping(ws)
                    continue
                if raw is None or raw == "":
                    raise ConnectionError("厂商关闭了连接")
                self._handle_vendor_message(raw)
        finally:
            self._ws = None
            try:
                ws.close()
            except Exception:  # noqa: BLE001
                pass

    def _proxy_kwargs(self) -> dict:
        """把 proxy_url 转成 websocket-client 的代理参数。"""
        proxy_url = (self.config.get("proxy_url") or "").strip()
        if not proxy_url:
            return {}
        parsed = urlparse(proxy_url)
        if not parsed.hostname:
            return {}
        kwargs = {
            "http_proxy_host": parsed.hostname,
            "http_proxy_port": parsed.port or (443 if parsed.scheme == "https" else 80),
            "proxy_type": "http",
        }
        if parsed.username:
            kwargs["http_proxy_auth"] = (parsed.username, parsed.password or "")
        return kwargs

    def _send_session_update(self, ws) -> None:
        """发送会话配置（转写、VAD、音色、系统提示词）。"""
        sample_rate = int(self.config.get("sample_rate") or 24000)
        payload = {
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": self.config.get("instructions") or DEFAULT_INSTRUCTIONS,
                "voice": self.config.get("voice") or "",
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_transcription": {"model": "whisper-1"},
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.5,
                    "prefix_padding_ms": 300,
                    "silence_duration_ms": 500,
                },
                "sample_rate": sample_rate,
            },
        }
        ws.send(json.dumps(payload, ensure_ascii=False))

    def _drain_outbound(self, ws) -> None:
        """把队列里的事件发给厂商。"""
        while True:
            with self._lock:
                if not self._outbound:
                    return
                payload = self._outbound.popleft()
            try:
                ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - 发送失败按断线处理
                with self._lock:
                    self._outbound.appendleft(payload)
                raise ConnectionError(f"向厂商发送失败：{exc}") from exc
            if payload.get("type") == "input_audio_buffer.append":
                self.stats.chunks_out += 1

    def _maybe_ping(self, ws) -> None:
        """长时间无消息时发 ping，保活并尽早发现断线。"""
        now = time.time()
        if now - self._last_ping < 20:
            return
        self._last_ping = now
        try:
            ws.ping()
        except Exception as exc:  # noqa: BLE001
            raise ConnectionError(f"ping 失败：{exc}") from exc

    # -- 厂商 -> 浏览器 ---------------------------------------------------

    def _handle_vendor_message(self, raw) -> None:
        """解析厂商事件并转成浏览器侧事件。"""
        if isinstance(raw, bytes):
            # Qwen/GLM 正常只发文本帧；二进制帧（原生音频）直接按音频转发
            self.stats.bytes_out += len(raw)
            self._emit("audio", {
                "delta": base64.b64encode(raw).decode("ascii"),
                "encoding": "pcm16",
            })
            return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            _log.debug("[Realtime] 无法解析厂商消息：%s", str(raw)[:200])
            return
        if not isinstance(event, dict):
            return

        event_type = str(event.get("type") or "")
        mapped = _map_vendor_event(event_type, event)
        if mapped is not None:
            name, payload = mapped
            if name == "audio":
                self.stats.chunks_out += 1
            elif name == "response_started":
                self._response_active = True
            elif name == "response_done":
                self._response_active = False
            self._emit(name, payload)


def _map_vendor_event(event_type: str, event: dict) -> tuple[str, dict] | None:
    """厂商事件 -> (浏览器事件名, 负载)；不关心的返回 None。"""
    if event_type in ("session.created", "session.updated"):
        return "session", {"type": event_type, "session": event.get("session") or {}}
    if event_type == "response.created":
        return "response_started", {"response_id": (event.get("response") or {}).get("id", "")}
    if event_type in ("response.audio.delta", "response.output_audio.delta"):
        delta = event.get("delta") or ""
        if not delta:
            return None
        return "audio", {"delta": delta, "encoding": "pcm16"}
    if event_type in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
        delta = event.get("delta") or ""
        return ("assistant_transcript_delta", {"delta": delta}) if delta else None
    if event_type in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
        return "assistant_transcript_done", {"text": event.get("transcript") or ""}
    if event_type == "conversation.item.input_audio_transcription.completed":
        return "user_transcript", {"text": event.get("transcript") or ""}
    if event_type == "input_audio_buffer.speech_started":
        return "speech_started", {"audio_start_ms": event.get("audio_start_ms", 0)}
    if event_type == "input_audio_buffer.speech_stopped":
        return "speech_stopped", {"audio_end_ms": event.get("audio_end_ms", 0)}
    if event_type == "response.done":
        response = event.get("response") or {}
        return "response_done", {
            "status": response.get("status", ""),
            "usage": response.get("usage") or {},
        }
    if event_type == "error":
        error = event.get("error") or {}
        return "error", {
            "message": error.get("message") or "厂商返回错误",
            "code": error.get("code", ""),
            "retryable": False,
        }
    return None


# ----------------------------------------------------------------------------
# 会话管理器 + Socket.IO 注册
# ----------------------------------------------------------------------------


class RealtimeVoiceManager:
    """按 Socket.IO sid 管理实时语音会话（一个连接一个会话）。"""

    def __init__(self):
        self._sessions: dict[str, RealtimeVoiceSession] = {}
        self._lock = threading.RLock()

    def start(self, sid: str, config: dict, *, emit, session_id: str = "") -> dict:
        self.end(sid, reason="replaced")
        session = RealtimeVoiceSession(emit=emit, config=config, session_id=session_id)
        with self._lock:
            self._sessions[sid] = session
        session.start()
        return session._stats_payload()

    def get(self, sid: str) -> RealtimeVoiceSession | None:
        with self._lock:
            return self._sessions.get(sid)

    def end(self, sid: str, reason: str = "client_stop") -> bool:
        with self._lock:
            session = self._sessions.pop(sid, None)
        if session is None:
            return False
        session.stop(reason=reason)
        return True

    def stats(self) -> dict:
        with self._lock:
            sessions = list(self._sessions.values())
        return {
            "active_sessions": len(sessions),
            "sessions": [session._stats_payload() for session in sessions],
        }


def _extract_token(auth) -> str:
    """从 Socket.IO auth / 请求头 / cookie 取登录 token。"""
    token = ""
    if isinstance(auth, dict):
        token = str(auth.get("token") or "").strip()
    if not token:
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
    if not token:
        token = (
            request.headers.get("X-Auth-Token", "").strip()
            or request.headers.get("X-Token", "").strip()
            or request.cookies.get("nbot_auth_token", "").strip()
        )
    return token


def register_realtime_voice(server, manager: RealtimeVoiceManager | None = None) -> bool:
    """注册 /realtime-voice 命名空间。已注册过则跳过。

    Returns:
        True 注册成功，False 没有 socketio 或重复注册。
    """
    socketio = getattr(server, "socketio", None)
    if socketio is None:
        _log.warning("[Realtime] 无 socketio，跳过实时语音注册")
        return False
    if getattr(server, "_realtime_voice_registered", False):
        return False

    manager = manager or RealtimeVoiceManager()
    server._realtime_voice_manager = manager
    server._realtime_voice_registered = True

    def _emit_to(sid: str, event: str, payload: dict) -> None:
        try:
            socketio.emit(event, payload, to=sid, namespace=REALTIME_NAMESPACE)
        except Exception as exc:  # noqa: BLE001 - 连接可能已断
            _log.debug("[Realtime] 推送 %s 失败: %s", event, exc)

    @socketio.on("connect", namespace=REALTIME_NAMESPACE)
    def _on_connect(auth=None):  # noqa: ANN001 - socketio 回调签名
        username = server._validate_login_token(_extract_token(auth))
        if not username:
            _log.warning("[Realtime] 拒绝未鉴权的实时语音连接")
            return False
        server.active_connections[f"rt:{request.sid}"] = username
        _log.info("[Realtime] 客户端已连接 sid=%s user=%s", request.sid, username)
        return None

    @socketio.on("disconnect", namespace=REALTIME_NAMESPACE)
    def _on_disconnect():  # noqa: ANN001
        server.active_connections.pop(f"rt:{request.sid}", None)
        manager.end(request.sid, reason="disconnected")
        _log.info("[Realtime] 客户端断开 sid=%s", request.sid)

    @socketio.on("realtime.start", namespace=REALTIME_NAMESPACE)
    def _on_start(data):  # noqa: ANN001
        data = data or {}
        try:
            config = resolve_realtime_config(
                provider_id=data.get("provider", ""),
                model=data.get("model", ""),
                voice=data.get("voice", ""),
                base_url=data.get("base_url", ""),
                instructions=data.get("instructions", ""),
                sample_rate=data.get("sample_rate", 0),
            )
        except RealtimeConfigError as exc:
            _emit_to(request.sid, "error", {"message": str(exc), "retryable": False})
            return
        except Exception as exc:  # noqa: BLE001
            _log.error("[Realtime] 启动会话失败: %s", exc, exc_info=True)
            _emit_to(request.sid, "error", {"message": str(exc), "retryable": False})
            return

        stats = manager.start(
            request.sid,
            config,
            emit=lambda event, payload: _emit_to(request.sid, event, payload),
            session_id=str(data.get("session_id") or ""),
        )
        _emit_to(request.sid, "starting", {
            "provider": config["provider"],
            "model": config["model"],
            "voice": config["voice"],
            "sample_rate": config["sample_rate"],
            "session": stats,
        })

    @socketio.on("realtime.audio", namespace=REALTIME_NAMESPACE)
    def _on_audio(data):  # noqa: ANN001
        session = manager.get(request.sid)
        if session is None:
            _emit_to(request.sid, "error", {"message": "会话未启动", "retryable": True})
            return
        audio = (data or {}).get("audio") or ""
        if not session.send_audio(str(audio)):
            _emit_to(request.sid, "error", {"message": "音频块无效或过大", "retryable": True})

    @socketio.on("realtime.commit", namespace=REALTIME_NAMESPACE)
    def _on_commit(data=None):  # noqa: ANN001
        session = manager.get(request.sid)
        if session is not None:
            session.commit()

    @socketio.on("realtime.interrupt", namespace=REALTIME_NAMESPACE)
    def _on_interrupt(data=None):  # noqa: ANN001
        session = manager.get(request.sid)
        if session is not None:
            session.interrupt()

    @socketio.on("realtime.stop", namespace=REALTIME_NAMESPACE)
    def _on_stop(data=None):  # noqa: ANN001
        manager.end(request.sid, reason="client_stop")

    return True
