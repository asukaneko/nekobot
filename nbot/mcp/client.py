"""MCP 远程客户端

连接远程 MCP Server（streamable-http），提供交互式 CLI 调用工具。

Usage:
    python bot.py --mcp-connect http://127.0.0.1:5001/mcp
"""

import asyncio
import json
import logging
import sys
from typing import Any

_log = logging.getLogger(__name__)


class MCPRemoteClient:
    """MCP 远程客户端

    支持两种传输方式：
    - streamable-http: 通过 HTTP URL 连接远程 MCP Server
    - stdio: 通过启动子进程（command + args）通信
    """

    def __init__(
        self,
        url: str = "",
        transport: str = "streamable-http",
        command: str = "",
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self._url = url
        self._transport = transport
        self._command = command
        self._args = args or []
        self._env = env
        self._headers = dict(headers or {})
        self._session: Any = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._exit_stack: Any = None

    async def connect(self) -> None:
        """连接远程 MCP Server"""
        from contextlib import AsyncExitStack
        from mcp import ClientSession

        self._exit_stack = AsyncExitStack()

        if self._transport == "stdio":
            from mcp.client.stdio import StdioServerParameters, stdio_client

            params = StdioServerParameters(
                command=self._command,
                args=self._args,
                env=self._env,
            )
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                stdio_client(params)
            )
            _log.info("[MCP Client] 已连接 stdio: %s %s", self._command, self._args)
        else:
            from mcp.client.streamable_http import streamablehttp_client

            read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                streamablehttp_client(self._url, headers=self._headers or None)
            )
            _log.info("[MCP Client] 已连接 HTTP %s", self._url)

        self._session = await self._exit_stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await self._session.initialize()

    async def close(self) -> None:
        """关闭连接"""
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None

    async def list_tools(self) -> list[dict[str, Any]]:
        """列出远程所有工具"""
        if not self._session:
            raise RuntimeError("未连接，请先调用 connect()")
        result = await self._session.list_tools()
        tools: list[dict[str, Any]] = []
        for t in result.tools:
            annotations = getattr(t, "annotations", None)
            if annotations is not None and hasattr(annotations, "model_dump"):
                try:
                    annotations = annotations.model_dump()
                except Exception:
                    annotations = None
            tools.append(
                {
                    "name": t.name,
                    "description": t.description or "",
                    "input_schema": t.inputSchema if hasattr(t, "inputSchema") else {},
                    # MCP 注解（readOnlyHint 等）：用于只读工具并行调度
                    "annotations": annotations if isinstance(annotations, dict) else {},
                }
            )
        return tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """调用远程工具"""
        if not self._session:
            raise RuntimeError("未连接，请先调用 connect()")
        result = await self._session.call_tool(name, arguments=arguments or {})
        # 提取文本内容
        contents = []
        for item in result.content:
            if hasattr(item, "text"):
                contents.append(item.text)
            else:
                contents.append(str(item))
        text = "\n".join(contents)
        # 尝试解析 JSON
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    async def list_resources(self) -> list[dict[str, Any]]:
        """列出远程所有资源"""
        if not self._session:
            raise RuntimeError("未连接，请先调用 connect()")
        result = await self._session.list_resources()
        return [
            {
                "uri": str(r.uri),
                "name": r.name,
                "description": r.description or "",
            }
            for r in result.resources
        ]

    async def interactive_loop(self) -> None:
        """交互式 CLI 循环"""
        print(f"\n🐱 NekoBot MCP Client — 已连接 {self._url}")
        print("输入 help 查看可用命令\n")

        while True:
            try:
                line = input("mcp> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见~")
                break

            if not line:
                continue

            parts = line.split(maxsplit=1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""

            try:
                if cmd in ("quit", "exit", "q"):
                    print("再见~")
                    break
                elif cmd in ("help", "h"):
                    self._print_help()
                elif cmd in ("tools", "t"):
                    await self._cmd_list_tools()
                elif cmd in ("resources", "r"):
                    await self._cmd_list_resources()
                elif cmd in ("call", "c"):
                    await self._cmd_call_tool(arg)
                else:
                    print(f"未知命令: {cmd}，输入 help 查看帮助")
            except Exception as e:
                print(f"错误: {e}")

    def _print_help(self) -> None:
        print("""
可用命令:
  tools (t)              列出所有可用工具
  resources (r)          列出所有可用资源
  call <tool> [json]     调用工具，json 为参数（可选）
  help (h)               显示帮助
  quit (q)               退出

示例:
  call gateway_get_status
  call gateway_query_events {"limit": 5}
  call gateway_send_message {"channel_id": "web", "conversation_id": "xxx", "content": "hello", "confirm": true}
""")

    async def _cmd_list_tools(self) -> None:
        tools = await self.list_tools()
        if not tools:
            print("无可用工具")
            return
        print(f"\n共 {len(tools)} 个工具:\n")
        for t in tools:
            print(f"  {t['name']}")
            if t["description"]:
                print(f"    {t['description']}")
        print()

    async def _cmd_list_resources(self) -> None:
        resources = await self.list_resources()
        if not resources:
            print("无可用资源")
            return
        print(f"\n共 {len(resources)} 个资源:\n")
        for r in resources:
            print(f"  {r['uri']}")
            if r["description"]:
                print(f"    {r['description']}")
        print()

    async def _cmd_call_tool(self, arg: str) -> None:
        if not arg:
            print("用法: call <tool_name> [json_arguments]")
            return

        parts = arg.split(maxsplit=1)
        tool_name = parts[0]
        arguments: dict[str, Any] = {}

        if len(parts) > 1:
            try:
                arguments = json.loads(parts[1])
            except json.JSONDecodeError as e:
                print(f"JSON 解析错误: {e}")
                return

        print(f"调用 {tool_name} ...")
        result = await self.call_tool(tool_name, arguments)
        print(json.dumps(result, ensure_ascii=False, indent=2) if isinstance(result, (dict, list)) else result)


async def run_mcp_connect(url: str) -> None:
    """启动 MCP 远程客户端"""
    client = MCPRemoteClient(url)
    try:
        await client.connect()
        await client.interactive_loop()
    finally:
        await client.close()
