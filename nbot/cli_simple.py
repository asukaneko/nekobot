"""
NekoBot CLI - 简化版命令行界面
类似Claude Code的交互式CLI
"""

import os
import sys
import json
import uuid
import requests
import copy
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.layout import Layout
from rich.live import Live
from rich.box import ROUNDED, HEAVY
from rich.prompt import Prompt
from rich.align import Align
from rich import box
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.tree import Tree
from rich.console import Group

# 尝试导入pyfiglet用于ASCII艺术字
try:
    from pyfiglet import Figlet
    PYFIGLET_AVAILABLE = True
except ImportError:
    PYFIGLET_AVAILABLE = False

# 导入项目核心模块
try:
    from nbot.services.tools import TOOL_DEFINITIONS, execute_tool
    TOOLS_AVAILABLE = True
except ImportError:
    TOOLS_AVAILABLE = False
    TOOL_DEFINITIONS = []

try:
    from nbot.core import (
        build_cli_session_id,
        build_chat_completion_payload,
        normalize_chat_completion_data,
        resolve_chat_completion_url,
    )
    CORE_AVAILABLE = True
except ImportError:
    CORE_AVAILABLE = False
    def build_cli_session_id():
        return f"cli_{uuid.uuid4().hex}"


def _silence_cli_loggers():
    """在 CLI 模式下屏蔽所有常见日志输出。"""
    logger_names = [
        "",
        "nbot",
        "werkzeug",
        "urllib3",
        "requests",
        "socketio",
        "engineio",
        "apscheduler",
    ]
    for name in logger_names:
        logger = logging.getLogger(name)
        logger.setLevel(logging.CRITICAL + 1)
        logger.propagate = False
        for handler in logger.handlers:
            handler.setLevel(logging.CRITICAL + 1)


_silence_cli_loggers()


class SimpleCLI:
    """简化版CLI - 使用简单的输入循环"""

    def __init__(self):
        self.console = Console()
        self.running = True
        self.current_screen = "main"
        self.session_id = None
        self.messages = []
        self.current_model_id = None
        self.available_models = []
        self.show_thinking = True  # 是否显示思考过程
        self.max_tool_iterations = 10  # 最大工具调用轮数

        # 确保数据目录存在
        os.makedirs(os.path.join("data", "web"), exist_ok=True)

        # 加载可用模型
        self._load_models()

    def _load_models(self):
        """加载可用模型列表"""
        models_file = os.path.join("data", "web", "ai_models.json")
        if os.path.exists(models_file):
            try:
                with open(models_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.available_models = [
                        m for m in data.get("models", [])
                        if m.get("enabled", True)
                    ]
                    self.current_model_id = data.get("active_model_id")
            except Exception as e:
                self.console.print(f"[dim]加载模型配置失败: {e}[/dim]")

    def _get_current_model(self) -> Optional[Dict]:
        """获取当前选中的模型"""
        for model in self.available_models:
            if model.get("id") == self.current_model_id:
                return model
        if self.available_models:
            return self.available_models[0]
        return None

    def _set_active_model(self, model_id: str):
        """设置活动模型"""
        self.current_model_id = model_id
        models_file = os.path.join("data", "web", "ai_models.json")
        try:
            with open(models_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            data["active_model_id"] = model_id
            with open(models_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.console.print(f"[red]保存模型配置失败: {e}[/red]")

    def _get_ascii_art(self, text: str = "NekoBot", font: str = "small") -> str:
        """生成ASCII艺术字"""
        if PYFIGLET_AVAILABLE:
            try:
                f = Figlet(font=font)
                return f.renderText(text)
            except:
                pass
        return r"""
 _  _     _       ___      _
| \| |___| |_____| _ ) ___| |_
| .` / -_) / / _ \ _ \/ _ \  _|
|_|\_\___|_\_\___/___/\___/\__|
        """

    def print_header(self):
        """打印标题"""
        self.console.print()
        ascii_art = self._get_ascii_art("NekoBot", "small")
        title_text = Text()
        title_text.append(ascii_art, style="bold cyan")
        title_text.append("\n")
        title_text.append("🐱 ", style="yellow")
        title_text.append("智能助手命令行界面", style="dim cyan")

        self.console.print(Panel(
            Align.center(title_text),
            box=HEAVY,
            style="cyan",
            padding=(1, 2)
        ))
        self.console.print()

    def print_main_menu(self):
        """打印主菜单"""
        table = Table(show_header=False, box=ROUNDED, style="cyan")
        table.add_column("选项", style="cyan", width=10)
        table.add_column("描述", style="white")

        table.add_row("[bold]1[/bold]", "💬 开始聊天")
        table.add_row("[bold]2[/bold]", "📜 查看会话")
        table.add_row("[bold]3[/bold]", "🔧 管理工具")
        table.add_row("[bold]4[/bold]", "⚙️  系统配置")
        table.add_row("[bold]5[/bold]", "🤖 切换AI模型")
        table.add_row("[bold]6[/bold]", "❓ 帮助信息")
        table.add_row("[bold]0[/bold]", "🚪 退出")

        self.console.print(table)

        current_model = self._get_current_model()
        if current_model:
            self.console.print(f"\n[dim]当前模型: [cyan]{current_model.get('name', 'Unknown')}[/cyan] ({current_model.get('model', 'Unknown')})[/dim]")

        if TOOLS_AVAILABLE:
            self.console.print(f"[dim]工具支持: [green]已启用[/green] ({len(TOOL_DEFINITIONS)} 个工具)[/dim]")
        else:
            self.console.print(f"[dim]工具支持: [yellow]未启用[/yellow][/dim]")

        self.console.print()

    def get_input(self, prompt: str = "请选择") -> str:
        """获取用户输入"""
        try:
            return Prompt.ask(f"[cyan]{prompt}[/cyan]").strip()
        except (KeyboardInterrupt, EOFError):
            return "0"

    def load_sessions(self) -> List[Dict]:
        """加载会话列表"""
        sessions_file = os.path.join("data", "web", "sessions.json")
        if os.path.exists(sessions_file):
            try:
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions_data = json.load(f)
                    return list(sessions_data.values())
            except:
                pass
        return []

    def load_tools(self) -> List[Dict]:
        """加载工具列表"""
        tools_file = os.path.join("data", "web", "tools.json")
        if os.path.exists(tools_file):
            try:
                with open(tools_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return []

    def save_session(self):
        """保存当前会话"""
        if not self.session_id or not self.messages:
            return

        try:
            sessions_file = os.path.join("data", "web", "sessions.json")
            sessions = {}

            if os.path.exists(sessions_file):
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions = json.load(f)

            session_name = "CLI会话"
            for msg in self.messages:
                if msg.get("role") == "user":
                    content = msg.get("content", "")
                    if len(content) > 30:
                        content = content[:30] + "..."
                    session_name = content
                    break

            session_data = {
                "id": self.session_id,
                "name": session_name,
                "type": "cli",
                "messages": [],
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }

            for i, msg in enumerate(self.messages):
                stored_message = copy.deepcopy(msg)
                stored_message.setdefault("id", str(i))
                stored_message.setdefault(
                    "timestamp", datetime.now().isoformat()
                )
                stored_message.setdefault(
                    "sender", "user" if stored_message.get("role") == "user" else "AI"
                )
                stored_message.setdefault("source", "cli")
                stored_message.setdefault("session_id", self.session_id)
                session_data["messages"].append(stored_message)

            sessions[self.session_id] = session_data

            os.makedirs(os.path.dirname(sessions_file), exist_ok=True)
            with open(sessions_file, 'w', encoding='utf-8') as f:
                json.dump(sessions, f, ensure_ascii=False, indent=2)

        except Exception as e:
            self.console.print(f"[red]保存会话失败: {e}[/red]")

    def _get_tool_definitions(self) -> List[Dict]:
        """获取工具定义列表"""
        if not TOOLS_AVAILABLE:
            return []
        return TOOL_DEFINITIONS

    def _execute_tool(self, tool_name: str, arguments: Dict) -> Dict:
        """执行工具"""
        if not TOOLS_AVAILABLE:
            return {"success": False, "error": "工具系统未启用"}

        try:
            tool_context = {"session_id": self.session_id} if self.session_id else {}
            result = execute_tool(tool_name, arguments, tool_context)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _render_markdown(self, content: str):
        """渲染Markdown内容"""
        if not content:
            return

        # 使用Rich的Markdown渲染
        md = Markdown(content, code_theme="monokai")
        self.console.print(md)

    def _render_thinking(self, thinking: str):
        """渲染思考过程"""
        if not thinking or not self.show_thinking:
            return

        # 创建可折叠的思考面板
        thinking_text = Text(thinking, style="dim italic")
        panel = Panel(
            thinking_text,
            title="[dim]💭 思考过程[/dim]",
            box=ROUNDED,
            style="dim",
            padding=(1, 2)
        )
        self.console.print(panel)

    def _render_tool_call(self, tool_name: str, arguments: Dict):
        """渲染工具调用"""
        args_str = json.dumps(arguments, ensure_ascii=False, indent=2)
        content = f"[bold cyan]{tool_name}[/bold cyan]\n{args_str}"

        panel = Panel(
            content,
            title="[yellow]🔧 调用工具[/yellow]",
            box=ROUNDED,
            style="yellow",
            padding=(1, 2)
        )
        self.console.print(panel)

    def _render_tool_result(self, result: Dict):
        """渲染工具执行结果"""
        success = result.get("success", False)
        style = "green" if success else "red"
        icon = "✓" if success else "✗"

        # 格式化结果
        if isinstance(result, dict):
            if "result" in result:
                content = str(result["result"])
            elif "error" in result:
                content = f"[red]错误: {result['error']}[/red]"
            else:
                content = json.dumps(result, ensure_ascii=False, indent=2)
        else:
            content = str(result)

        # 截断过长的结果
        if len(content) > 500:
            content = content[:500] + "\n... (结果已截断)"

        panel = Panel(
            content,
            title=f"[{style}]{icon} 工具结果[/{style}]",
            box=ROUNDED,
            style=style,
            padding=(1, 2)
        )
        self.console.print(panel)

    def _expand_messages_for_ai(self, messages: List[Dict]) -> List[Dict]:
        """展开隐藏保存的工具历史，保证后续请求能看到它们。"""
        expanded_messages = []
        for msg in copy.deepcopy(messages):
            hidden_tool_history = msg.pop("tool_call_history", None)
            expanded_messages.append(msg)
            if not isinstance(hidden_tool_history, list):
                continue
            for hidden_msg in hidden_tool_history:
                if not isinstance(hidden_msg, dict):
                    continue
                if hidden_msg.get("role") not in ("assistant", "tool"):
                    continue
                expanded_messages.append(copy.deepcopy(hidden_msg))
        return expanded_messages

    def _extract_turn_tool_history(
        self, tool_messages: List[Dict], initial_message_count: int
    ) -> List[Dict]:
        return [
            copy.deepcopy(msg)
            for msg in tool_messages[initial_message_count:]
            if msg.get("role") in ("assistant", "tool")
        ]

    def _call_ai_with_tools(self, messages: List[Dict]) -> Dict[str, Any]:
        """调用AI，支持工具调用和多轮思考"""
        model = self._get_current_model()
        if not model:
            return {"content": "错误：没有可用的AI模型。请先配置模型。", "thinking": "", "tool_calls": []}

        try:
            api_key = model.get("api_key", "")
            base_url = model.get("base_url", "")
            model_name = model.get("model", "")
            provider_type = model.get("provider_type", "openai_compatible")
            supports_tools = model.get("supports_tools", True)

            if not api_key or not base_url:
                return {"content": "错误：模型配置不完整（缺少API密钥或基础URL）", "thinking": "", "tool_calls": []}

            # 处理URL
            url = base_url.rstrip("/")
            if "minimaxi.com" in url:
                pass
            elif "siliconflow.cn" in url:
                url += "/chat/completions"
            elif "/v1" not in url:
                url += "/v1/chat/completions"
            else:
                url += "/chat/completions"

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            # 获取工具定义
            tools = self._get_tool_definitions() if supports_tools else []

            # 准备消息
            tool_messages = self._expand_messages_for_ai(messages)
            initial_message_count = len(tool_messages)
            all_thinking = []
            all_tool_calls = []

            # 多轮工具调用循环
            for iteration in range(self.max_tool_iterations):
                payload = {
                    "model": model_name,
                    "messages": tool_messages,
                    "temperature": model.get("temperature", 0.7),
                    "max_tokens": model.get("max_tokens", 2000),
                    "stream": False
                }

                # 添加工具支持
                if tools and supports_tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"

                response = requests.post(url, headers=headers, json=payload, timeout=120)
                response.raise_for_status()
                data = response.json()

                # 解析响应
                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})

                # 提取思考内容（如果支持）
                thinking = message.get("reasoning_content", "") or message.get("thinking", "")
                if thinking:
                    all_thinking.append(f"第{iteration + 1}轮: {thinking}")

                # 检查是否有工具调用
                if message.get("tool_calls") and supports_tools:
                    tool_calls = message["tool_calls"]
                    all_tool_calls.extend(tool_calls)

                    # 添加AI回复到消息历史
                    tool_messages.append({
                        "role": "assistant",
                        "content": message.get("content", ""),
                        "tool_calls": [
                            {
                                "id": tc.get("id"),
                                "type": "function",
                                "function": {
                                    "name": tc.get("function", {}).get("name"),
                                    "arguments": tc.get("function", {}).get("arguments")
                                }
                            } for tc in tool_calls
                        ]
                    })

                    # 执行工具调用
                    for tool_call in tool_calls:
                        tool_name = tool_call.get("function", {}).get("name")
                        try:
                            arguments = json.loads(tool_call.get("function", {}).get("arguments", "{}"))
                        except:
                            arguments = {}

                        # 显示工具调用
                        self._render_tool_call(tool_name, arguments)

                        # 执行工具
                        result = self._execute_tool(tool_name, arguments)

                        # 显示工具结果
                        self._render_tool_result(result)

                        # 添加工具结果到消息历史
                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "content": json.dumps(result, ensure_ascii=False)
                        })

                else:
                    # 没有工具调用，得到最终回复
                    final_content = message.get("content", "")

                    # 处理不同格式的响应
                    if not final_content and "base_resp" in data:
                        final_content = data.get("reply", "")

                    return {
                        "content": final_content,
                        "thinking": "\n\n".join(all_thinking),
                        "tool_calls": all_tool_calls,
                        "tool_call_history": self._extract_turn_tool_history(tool_messages, initial_message_count),
                        "iterations": iteration + 1
                    }

            # 超过最大迭代次数
            return {
                "content": "工具调用次数过多，已停止。请简化您的请求。",
                "thinking": "\n\n".join(all_thinking),
                "tool_calls": all_tool_calls,
                "tool_call_history": self._extract_turn_tool_history(tool_messages, initial_message_count),
                "iterations": self.max_tool_iterations
            }

        except requests.exceptions.RequestException as e:
            return {"content": f"网络错误：{str(e)}", "thinking": "", "tool_calls": []}
        except Exception as e:
            return {"content": f"调用AI出错：{str(e)}", "thinking": "", "tool_calls": []}

    def chat_mode(self):
        """聊天模式"""
        self.console.clear()
        self.print_header()

        if not self.session_id:
            self.session_id = build_cli_session_id()
            self.messages = []

        current_model = self._get_current_model()
        if current_model:
            self.console.print(f"[dim]当前模型: [cyan]{current_model.get('name', 'Unknown')}[/cyan][/dim]")
        else:
            self.console.print("[yellow]警告：没有配置AI模型，聊天功能不可用[/yellow]")

        if TOOLS_AVAILABLE:
            self.console.print(f"[dim]工具支持: [green]已启用[/green] - 输入 /tools 查看可用工具[/dim]")

        self.console.print("[dim]输入 /back 返回主菜单，/help 查看帮助[/dim]")
        self.console.print()

        while True:
            try:
                user_input = Prompt.ask("[bold green]你[/bold green]").strip()

                if not user_input:
                    continue

                if user_input == "/back":
                    self.save_session()
                    break
                elif user_input == "/quit" or user_input == "/exit":
                    self.save_session()
                    self.running = False
                    return
                elif user_input == "/clear":
                    self.messages = []
                    self.console.print("[dim]消息已清空[/dim]")
                    continue
                elif user_input == "/help":
                    self.console.print("""
[dim]可用命令：
/back - 返回主菜单
/clear - 清空消息
/help - 显示帮助
/quit - 退出CLI
/model - 切换模型
/tools - 查看可用工具
/thinking - 切换思考过程显示[/dim]
""")
                    continue
                elif user_input == "/model":
                    self._switch_model_in_chat()
                    continue
                elif user_input == "/tools":
                    self._show_tools_in_chat()
                    continue
                elif user_input == "/thinking":
                    self.show_thinking = not self.show_thinking
                    status = "开启" if self.show_thinking else "关闭"
                    self.console.print(f"[dim]思考过程显示已{status}[/dim]")
                    continue

                # 添加用户消息到历史
                self.messages.append({
                    "role": "user",
                    "content": user_input,
                    "timestamp": datetime.now().isoformat()
                })

                # 调用AI获取回复（支持工具调用和多轮思考）
                with self.console.status("[cyan]AI思考中...[/cyan]", spinner="dots"):
                    result = self._call_ai_with_tools(self.messages)

                content = result.get("content", "")
                thinking = result.get("thinking", "")
                tool_calls = result.get("tool_calls", [])
                tool_call_history = result.get("tool_call_history", [])
                iterations = result.get("iterations", 1)

                # 显示思考过程
                if thinking and self.show_thinking:
                    self._render_thinking(thinking)

                # 显示迭代信息（如果有工具调用）
                if tool_calls:
                    self.console.print(f"[dim]完成 {iterations} 轮工具调用，共 {len(tool_calls)} 个工具[/dim]")

                # 添加AI消息
                assistant_message = {
                    "role": "assistant",
                    "content": content,
                    "timestamp": datetime.now().isoformat()
                }
                if tool_call_history:
                    assistant_message["tool_call_history"] = tool_call_history
                self.messages.append(assistant_message)

                # 显示AI回复（使用Markdown渲染）
                self.console.print(f"[bold cyan]🐱 NekoBot:[/bold cyan]")
                self._render_markdown(content)
                self.console.print()

                # 自动保存会话
                self.save_session()

            except (KeyboardInterrupt, EOFError):
                self.save_session()
                break

    def _show_tools_in_chat(self):
        """在聊天中显示可用工具"""
        if not TOOLS_AVAILABLE or not TOOL_DEFINITIONS:
            self.console.print("[yellow]没有可用的工具[/yellow]")
            return

        self.console.print(f"\n[bold cyan]可用工具 ({len(TOOL_DEFINITIONS)}):[/bold cyan]\n")

        for tool in TOOL_DEFINITIONS:
            func = tool.get("function", {})
            name = func.get("name", "Unknown")
            description = func.get("description", "")
            params = func.get("parameters", {})
            required = params.get("required", [])

            self.console.print(f"  [bold]{name}[/bold]")
            self.console.print(f"    {description}")
            if required:
                self.console.print(f"    [dim]必需参数: {', '.join(required)}[/dim]")
            self.console.print()

    def _switch_model_in_chat(self):
        """在聊天中切换模型"""
        self.console.print("\n[bold cyan]可用模型：[/bold cyan]")
        for i, model in enumerate(self.available_models, 1):
            marker = "✓" if model.get("id") == self.current_model_id else " "
            self.console.print(f"  [{marker}] {i}. {model.get('name', 'Unknown')} - {model.get('model', 'Unknown')}")

        choice = self.get_input("选择模型编号 (或按Enter取消)")
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(self.available_models):
                model = self.available_models[idx]
                self._set_active_model(model.get("id"))
                self.console.print(f"[green]已切换到模型: {model.get('name')}[/green]")
            else:
                self.console.print("[red]无效的选择[/red]")

    def show_sessions(self):
        """显示会话列表"""
        self.console.clear()
        self.print_header()

        sessions = self.load_sessions()

        if not sessions:
            self.console.print("[dim]暂无会话[/dim]")
        else:
            table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
            table.add_column("ID", style="dim", width=12)
            table.add_column("名称", style="cyan")
            table.add_column("类型", style="blue", width=10)
            table.add_column("消息数", style="green", width=10)
            table.add_column("更新时间", style="dim", width=20)

            for session in sessions[:20]:
                session_id = session.get("id", "")[:8]
                name = session.get("name", "未命名")
                session_type = session.get("type", "unknown")
                message_count = len(session.get("messages", []))
                updated_at = session.get("updated_at", "")

                if updated_at:
                    try:
                        dt = datetime.fromisoformat(updated_at)
                        updated_at = dt.strftime("%Y-%m-%d %H:%M")
                    except:
                        pass

                table.add_row(session_id, name, session_type, str(message_count), updated_at)

            self.console.print(table)

        self.console.print()
        self.get_input("按Enter返回")

    def show_tools(self):
        """显示工具列表"""
        self.console.clear()
        self.print_header()

        if not TOOLS_AVAILABLE or not TOOL_DEFINITIONS:
            self.console.print("[dim]暂无可用工具[/dim]")
            self.console.print()
            self.get_input("按Enter返回")
            return

        self.console.print(f"[bold cyan]可用工具 ({len(TOOL_DEFINITIONS)}):[/bold cyan]\n")

        for tool in TOOL_DEFINITIONS:
            func = tool.get("function", {})
            name = func.get("name", "Unknown")
            description = func.get("description", "")
            params = func.get("parameters", {})
            properties = params.get("properties", {})
            required = params.get("required", [])

            # 创建工具详情面板
            content = f"[bold]{name}[/bold]\n{description}\n\n"

            if properties:
                content += "[dim]参数:[/dim]\n"
                for param_name, param_info in properties.items():
                    param_type = param_info.get("type", "any")
                    param_desc = param_info.get("description", "")
                    is_required = param_name in required
                    req_marker = "[red]*[/red]" if is_required else ""
                    content += f"  • {param_name}{req_marker}: {param_type} - {param_desc}\n"

            panel = Panel(
                content,
                box=ROUNDED,
                style="cyan",
                padding=(1, 2)
            )
            self.console.print(panel)
            self.console.print()

        self.get_input("按Enter返回")

    def show_config(self):
        """显示配置"""
        self.console.clear()
        self.print_header()

        configs = {}

        ai_config_file = os.path.join("data", "web", "ai_config.json")
        if os.path.exists(ai_config_file):
            try:
                with open(ai_config_file, 'r', encoding='utf-8') as f:
                    configs["AI配置"] = json.load(f)
            except:
                pass

        settings_file = os.path.join("data", "web", "settings.json")
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    configs["系统设置"] = json.load(f)
            except:
                pass

        if not configs:
            self.console.print("[dim]暂无配置信息[/dim]")
        else:
            for category, items in configs.items():
                self.console.print(f"\n[bold cyan]{category}[/bold cyan]")
                table = Table(show_header=False, box=ROUNDED)
                table.add_column("配置项", style="cyan", width=25)
                table.add_column("值", style="white")

                for key, value in items.items():
                    value_str = str(value)
                    if len(value_str) > 50:
                        value_str = value_str[:50] + "..."
                    table.add_row(key, value_str)

                self.console.print(table)

        self.console.print()
        self.get_input("按Enter返回")

    def switch_model(self):
        """切换AI模型"""
        self.console.clear()
        self.print_header()

        if not self.available_models:
            self.console.print("[red]没有可用的AI模型[/red]")
            self.console.print("[dim]请在Web界面配置模型后再试[/dim]")
            self.console.print()
            self.get_input("按Enter返回")
            return

        self.console.print("[bold cyan]可用模型：[/bold cyan]\n")

        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("编号", style="dim", width=6)
        table.add_column("名称", style="cyan")
        table.add_column("模型", style="white")
        table.add_column("提供商", style="blue", width=15)
        table.add_column("工具", style="green", width=8)
        table.add_column("思考", style="magenta", width=8)
        table.add_column("状态", style="green", width=10)

        for i, model in enumerate(self.available_models, 1):
            name = model.get("name", "Unknown")
            model_name = model.get("model", "Unknown")
            provider = model.get("provider_type", model.get("provider", "Unknown"))
            supports_tools = "✓" if model.get("supports_tools", True) else "✗"
            supports_reasoning = "✓" if model.get("supports_reasoning", True) else "✗"
            is_active = model.get("id") == self.current_model_id
            status = "✓ 当前使用" if is_active else ""
            status_style = "bold green" if is_active else ""

            table.add_row(
                str(i),
                f"[bold]{name}[/bold]" if is_active else name,
                model_name,
                provider,
                supports_tools,
                supports_reasoning,
                f"[{status_style}]{status}[/{status_style}]"
            )

        self.console.print(table)
        self.console.print()

        choice = self.get_input("选择模型编号 (或按Enter取消)")
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(self.available_models):
                model = self.available_models[idx]
                self._set_active_model(model.get("id"))
                self.console.print(f"\n[green]✓ 已切换到模型: {model.get('name')}[/green]")
            else:
                self.console.print("[red]无效的选择[/red]")

        self.console.print()
        self.get_input("按Enter返回")

    def show_help(self):
        """显示帮助"""
        self.console.clear()
        self.print_header()

        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("命令", style="cyan", width=15)
        table.add_column("说明", style="white")

        commands = {
            "/help": "显示帮助信息",
            "/quit": "退出CLI",
            "/clear": "清空屏幕",
            "/back": "返回上一级",
            "/model": "切换AI模型（聊天中）",
            "/tools": "查看可用工具",
            "/thinking": "切换思考过程显示",
        }

        for cmd, desc in commands.items():
            table.add_row(cmd, desc)

        self.console.print(table)
        self.console.print()

        self.console.print("[bold cyan]功能特性：[/bold cyan]")
        self.console.print("  • [green]多轮工具调用[/green] - AI可以自动调用多个工具完成任务")
        self.console.print("  • [green]思考过程显示[/green] - 查看AI的推理过程")
        self.console.print("  • [green]Markdown渲染[/green] - 美观的代码块和格式")
        self.console.print("  • [green]会话自动保存[/green] - 对话记录同步到Web端")
        self.console.print()

        self.console.print("[bold cyan]菜单选项说明：[/bold cyan]")
        self.console.print("  [bold]1[/bold] - 进入聊天模式，与AI对话")
        self.console.print("  [bold]2[/bold] - 查看历史会话列表")
        self.console.print("  [bold]3[/bold] - 查看和管理可用工具")
        self.console.print("  [bold]4[/bold] - 查看系统配置")
        self.console.print("  [bold]5[/bold] - 切换AI模型")
        self.console.print("  [bold]6[/bold] - 显示此帮助信息")
        self.console.print("  [bold]0[/bold] - 退出CLI程序")
        self.console.print()

        self.get_input("按Enter返回")

    def run(self):
        """运行CLI"""
        try:
            while self.running:
                self.console.clear()
                self.print_header()
                self.print_main_menu()

                choice = self.get_input("请选择操作")

                if choice == "0":
                    self.running = False
                elif choice == "1":
                    self.chat_mode()
                elif choice == "2":
                    self.show_sessions()
                elif choice == "3":
                    self.show_tools()
                elif choice == "4":
                    self.show_config()
                elif choice == "5":
                    self.switch_model()
                elif choice == "6":
                    self.show_help()
                elif choice.lower() in ["q", "quit", "exit"]:
                    self.running = False
                else:
                    self.console.print("[red]无效的选择，请重试[/red]")
                    import time
                    time.sleep(1)

        except KeyboardInterrupt:
            pass
        finally:
            self.save_session()
            self.console.print("\n[dim]再见！喵~ 🐱[/dim]")


def main():
    """CLI入口"""
    cli = SimpleCLI()
    cli.run()


if __name__ == "__main__":
    main()
