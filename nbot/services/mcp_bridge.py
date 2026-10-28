"""MCP Bridge

管理多个 MCP Server 连接，将 MCP 工具桥接为 AI 可用的 OpenAI function calling 格式。

工具命名规则：mcp__<server_id_short>__<tool_name>
  - server_id_short: UUID 前 8 位
  - tool_name: MCP 原始工具名

用法：
    bridge = get_mcp_bridge()
    bridge.run_async(bridge.connect_server("uuid-xxx", {"transport": "streamable-http", "url": "..."}))
    tools = bridge.get_openai_tool_definitions("uuid-xxx")
    result = bridge.run_async(bridge.execute("uuid-xxx", "tool_name", {"arg": "val"}))
"""

import asyncio
import concurrent.futures
import logging
import threading
from typing import Any

from nbot.mcp.client import MCPRemoteClient

_log = logging.getLogger(__name__)

# 工具名前缀分隔符
_SEP = "__"
_PREFIX = "mcp"

# 默认调用超时（秒）与单 server 并发上限
DEFAULT_CALL_TIMEOUT = 60.0
DEFAULT_MAX_CONCURRENCY_PER_SERVER = 4


class MCPCallTimeout(TimeoutError):
    """MCP 调用超时：远端协程已请求取消，连接已断开，下次调用自动重连。"""


def _make_tool_name(server_id: str, tool_name: str) -> str:
    """生成 MCP 工具的全局唯一名称"""
    short = server_id.replace("-", "")[:8]
    return f"{_PREFIX}{_SEP}{short}{_SEP}{tool_name}"


def _parse_tool_name(full_name: str) -> tuple[str, str] | None:
    """解析 MCP 工具名，返回 (server_id_short, tool_name) 或 None"""
    if not full_name.startswith(_PREFIX + _SEP):
        return None
    parts = full_name.split(_SEP, 2)
    if len(parts) < 3:
        return None
    return parts[1], parts[2]


def _tool_read_only_hint(tool: dict) -> bool:
    """读取 MCP 工具注解里的 readOnlyHint（缺省按有副作用处理）。"""
    annotations = tool.get("annotations")
    if isinstance(annotations, dict):
        return bool(annotations.get("readOnlyHint"))
    return False


def mcp_tool_to_openai(tool: dict, server_id: str) -> dict:
    """将 MCP 工具定义转为 OpenAI function calling 格式

    MCP 格式: {"name": "...", "description": "...", "input_schema": {...}}
    OpenAI 格式: {"type": "function", "function": {"name": "...", "description": "...", "parameters": {...}}}
    """
    name = _make_tool_name(server_id, tool["name"])
    parameters = tool.get("input_schema", {})
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    # 确保有 type 字段
    if "type" not in parameters:
        parameters["type"] = "object"
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": tool.get("description", ""),
            "parameters": parameters,
        },
    }


class MCPBridge:
    """MCP 连接管理器和工具桥接器"""

    def __init__(self):
        # server_id -> MCPRemoteClient
        self._clients: dict[str, MCPRemoteClient] = {}
        # server_id -> list[mcp_tool_dict]
        self._tool_cache: dict[str, list[dict]] = {}
        # server_id -> 连接配置（用于超时断连后的懒重连）
        self._configs: dict[str, dict] = {}
        # server_id -> 单 server 并发信号量（在事件循环线程内创建）
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        # server_id -> 最近一次错误（状态观测用）
        self._last_errors: dict[str, str] = {}
        # server_id -> 声明了 readOnlyHint 的工具名集合（参与只读并行）
        self._read_only_tools: dict[str, set[str]] = {}
        self._max_concurrency = DEFAULT_MAX_CONCURRENCY_PER_SERVER
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._loop_ready = threading.Event()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """确保持久化事件循环已启动"""
        if self._loop is not None and self._loop.is_running():
            return self._loop

        def _run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop_ready.set()
            self._loop.run_forever()

        self._loop_thread = threading.Thread(target=_run, name="mcp-bridge-loop", daemon=True)
        self._loop_thread.start()
        self._loop_ready.wait(timeout=10)
        return self._loop

    def run_async(
        self,
        coro,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
        *,
        cancel_on_timeout: bool = True,
    ):
        """在持久化事件循环中运行异步协程，同步等待结果。

        超时不再“只丢结果”：默认会取消远端协程（协作式取消，握手中的
        请求会被中断），并抛出 MCPCallTimeout，由调用方转成工具错误回给模型。
        timeout=None 表示不限制。
        """
        loop = self._ensure_loop()
        future: concurrent.futures.Future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            if cancel_on_timeout:
                cancelled = future.cancel()
                _log.warning(
                    "[MCP Bridge] 调用超时（%ss），已请求取消远端协程（cancel=%s）",
                    timeout,
                    cancelled,
                )
            else:
                _log.warning("[MCP Bridge] 调用超时（%ss），未取消远端协程", timeout)
            raise MCPCallTimeout(f"MCP 调用超时（{timeout}s，已取消）") from None

    def _get_semaphore(self, server_id: str) -> asyncio.Semaphore:
        """获取单 server 并发信号量（必须在事件循环线程内调用）。"""
        semaphore = self._semaphores.get(server_id)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self._max_concurrency)
            self._semaphores[server_id] = semaphore
        return semaphore

    def set_max_concurrency(self, limit: int) -> None:
        """调整单 server 并发上限（对之后新建的信号量生效）。"""
        if limit < 1:
            raise ValueError("limit 必须 >= 1")
        with self._lock:
            self._max_concurrency = limit
            self._semaphores = {
                sid: asyncio.Semaphore(limit) for sid in self._semaphores
            }

    async def _drop_client(self, server_id: str, reason: str) -> None:
        """超时/异常后断开连接：丢弃客户端，保留工具缓存，下次调用懒重连。"""
        with self._lock:
            client = self._clients.pop(server_id, None)
            self._last_errors[server_id] = reason
        if client is None:
            return
        try:
            await asyncio.wait_for(client.close(), timeout=5)
        except BaseException as exc:  # 关闭失败也要保证连接被丢弃
            _log.debug("[MCP Bridge] 断开 %s 失败: %s", server_id, exc)
        _log.warning("[MCP Bridge] 已断开 %s：%s", server_id, reason)

    async def _ensure_client(self, server_id: str) -> MCPRemoteClient | None:
        """拿可用客户端；被超时断连过就按保存的配置重连一次。"""
        with self._lock:
            client = self._clients.get(server_id)
            config = self._configs.get(server_id)
        if client is not None:
            return client
        if not config:
            return None
        _log.info("[MCP Bridge] %s 未连接，尝试按已保存配置重连", server_id)
        result = await self.connect_server(server_id, config)
        if not result.get("ok"):
            return None
        with self._lock:
            return self._clients.get(server_id)

    async def connect_server(self, server_id: str, config: dict) -> dict:
        """连接到 MCP Server 并缓存工具列表

        Args:
            server_id: 服务器 ID
            config: 连接配置，支持两种格式：
                HTTP: {"transport": "streamable-http", "url": "http://...", "headers": {...}}
                Stdio: {"transport": "stdio", "command": "python", "args": ["..."], "env": {...}}

        Returns:
            {"ok": True, "tool_count": N} 或 {"ok": False, "error": "..."}
        """
        # 先断开已有的
        await self.disconnect_server(server_id)

        transport = config.get("transport", "streamable-http")
        if transport == "stdio":
            client = MCPRemoteClient(
                transport="stdio",
                command=config.get("command", ""),
                args=config.get("args", []),
                env=config.get("env"),
            )
        else:
            client = MCPRemoteClient(
                transport="streamable-http",
                url=config.get("url", ""),
                headers=config.get("headers") if isinstance(config.get("headers"), dict) else None,
            )

        try:
            await client.connect()
            _log.info("[MCP Bridge] connect() 完成，开始 list_tools...")
            tools = await client.list_tools()
            _log.info("[MCP Bridge] list_tools() 返回 %d 个工具", len(tools))
        except BaseException as e:
            _log.error("[MCP Bridge] 连接失败 %s: %s (%s)", config, e, type(e).__name__, exc_info=True)
            try:
                await client.close()
            except BaseException:
                pass
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        with self._lock:
            self._clients[server_id] = client
            self._tool_cache[server_id] = tools
            self._configs[server_id] = dict(config)
            self._read_only_tools[server_id] = {
                tool["name"] for tool in tools if _tool_read_only_hint(tool)
            }
            self._last_errors.pop(server_id, None)

        _log.info("[MCP Bridge] 已连接 %s (%s), %d 个工具", server_id, transport, len(tools))
        return {"ok": True, "tool_count": len(tools)}

    async def disconnect_server(self, server_id: str) -> None:
        """断开指定服务器（显式断开不保留重连配置）"""
        with self._lock:
            client = self._clients.pop(server_id, None)
            self._tool_cache.pop(server_id, None)
            self._configs.pop(server_id, None)
            self._semaphores.pop(server_id, None)
            self._read_only_tools.pop(server_id, None)
        if client:
            try:
                await client.close()
            except Exception:
                pass
            _log.info("[MCP Bridge] 已断开 %s", server_id)

    async def disconnect_all(self) -> None:
        """断开所有服务器"""
        server_ids = list(self._clients.keys())
        for sid in server_ids:
            await self.disconnect_server(sid)

    def is_connected(self, server_id: str) -> bool:
        """检查是否已连接"""
        return server_id in self._clients

    def get_server_tools(self, server_id: str) -> list[dict]:
        """获取指定服务器的 MCP 格式工具列表"""
        return list(self._tool_cache.get(server_id, []))

    def get_openai_tool_definitions(self, server_id: str) -> list[dict]:
        """获取指定服务器的 OpenAI 格式工具定义"""
        tools = self._tool_cache.get(server_id, [])
        return [mcp_tool_to_openai(t, server_id) for t in tools]

    def get_all_openai_tools(self) -> list[dict]:
        """获取所有已连接服务器的 OpenAI 格式工具定义"""
        all_tools = []
        with self._lock:
            for server_id, tools in self._tool_cache.items():
                all_tools.extend(mcp_tool_to_openai(t, server_id) for t in tools)
        return all_tools

    def is_tool_read_only(self, full_name: str) -> bool:
        """判断 MCP 工具是否声明为只读（readOnlyHint），用于只读并行调度。"""
        parsed = _parse_tool_name(full_name)
        if not parsed:
            return False
        server_short, tool_name = parsed
        with self._lock:
            for server_id, names in self._read_only_tools.items():
                if server_id.replace("-", "")[:8] == server_short:
                    return tool_name in names
        return False

    async def execute(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict,
        timeout: float | None = None,
    ) -> Any:
        """执行 MCP 工具调用

        同一 server 允许并发（上限 DEFAULT_MAX_CONCURRENCY_PER_SERVER），
        单次调用可带 timeout；超时会取消该次调用并断开连接，下次调用懒重连。

        Returns:
            工具返回的结果（dict 或 str）
        """
        client = await self._ensure_client(server_id)
        if client is None:
            return {"ok": False, "error": f"MCP server {server_id} not connected"}

        semaphore = self._get_semaphore(server_id)
        async with semaphore:
            try:
                if timeout is not None:
                    result = await asyncio.wait_for(
                        client.call_tool(tool_name, arguments), timeout=timeout
                    )
                else:
                    result = await client.call_tool(tool_name, arguments)
                return result
            except asyncio.TimeoutError:
                await self._drop_client(
                    server_id, f"工具 {tool_name} 调用超时（{timeout}s）"
                )
                return {
                    "ok": False,
                    "error": f"MCP 工具 {tool_name} 调用超时（{timeout}s）",
                    "timeout": True,
                }
            except asyncio.CancelledError:
                # 外层 run_async 超时取消：让取消继续向上传播，连接由外层断连
                raise
            except Exception as e:
                _log.error("[MCP Bridge] 工具调用失败 %s/%s: %s", server_id, tool_name, e)
                return {"ok": False, "error": str(e)}

    async def execute_by_full_name(
        self,
        full_name: str,
        arguments: dict,
        timeout: float | None = None,
    ) -> Any:
        """通过全局工具名执行

        解析 mcp__<server_short>__<tool_name> 格式，找到对应 server 并调用。
        """
        parsed = _parse_tool_name(full_name)
        if not parsed:
            return {"ok": False, "error": f"Not an MCP tool: {full_name}"}

        server_short, tool_name = parsed

        # 找到匹配的 server_id
        with self._lock:
            server_id = None
            for sid in list(self._clients) + list(self._configs):
                if sid.replace("-", "")[:8] == server_short:
                    server_id = sid
                    break
        if not server_id:
            return {"ok": False, "error": f"MCP server not found for {full_name}"}

        return await self.execute(server_id, tool_name, arguments, timeout=timeout)

    def execute_sync(
        self,
        server_id: str,
        tool_name: str,
        arguments: dict,
        timeout: float | None = DEFAULT_CALL_TIMEOUT,
    ) -> Any:
        """同步入口：带超时取消地执行一次 MCP 工具调用。"""
        return self.run_async(
            self.execute(server_id, tool_name, arguments, timeout=timeout),
            timeout=(timeout + 5) if timeout is not None else None,
        )

    def get_status(self) -> dict:
        """获取所有服务器状态"""
        status = {}
        with self._lock:
            server_ids = (
                list(self._clients.keys())
                + list(self._tool_cache.keys())
                + list(self._configs.keys())
            )
            for server_id in server_ids:
                if server_id in status:
                    continue
                status[server_id] = {
                    "connected": server_id in self._clients,
                    "tool_count": len(self._tool_cache.get(server_id, [])),
                    "last_error": self._last_errors.get(server_id, ""),
                    "concurrency": self._max_concurrency,
                }
        return status


# 全局单例
_bridge: MCPBridge | None = None
_bridge_lock = threading.Lock()


def get_mcp_bridge() -> MCPBridge:
    """获取全局 MCPBridge 单例"""
    global _bridge
    if _bridge is None:
        with _bridge_lock:
            if _bridge is None:
                _bridge = MCPBridge()
    return _bridge
