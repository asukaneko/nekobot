"""
NekoBot CLI - Claude Code 风格
直接进入输入界面，所有操作通过命令完成
支持打断、对话队列、思考过程内联显示
"""

import os
import sys
import json
import uuid
import requests
import copy
import logging
import threading
import queue
import time
from datetime import datetime
from typing import Dict, Any, List, Optional
from concurrent.futures import ThreadPoolExecutor, Future

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.box import ROUNDED, HEAVY, DOUBLE
from rich.prompt import Prompt
from rich.align import Align
from rich import box
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.layout import Layout
from rich.live import Live
from rich.status import Status

# 屏蔽 nbot 相关的日志输出，避免干扰 CLI 界面
logging.getLogger("nbot").setLevel(logging.WARNING)
logging.getLogger("nbot.services.tools").setLevel(logging.WARNING)
logging.getLogger("nbot.services.dynamic_executor").setLevel(logging.WARNING)

# 尝试导入pyfiglet用于ASCII艺术字
try:
    from pyfiglet import Figlet
    PYFIGLET_AVAILABLE = True
except ImportError:
    PYFIGLET_AVAILABLE = False

# 尝试导入prompt_toolkit用于实时候选
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.key_binding import KeyBindings
    
    class CommandCompleter(Completer):
        """命令补全器 - 用于prompt_toolkit"""
        
        def __init__(self, commands):
            self.commands = commands
        
        def get_completions(self, document, complete_event):
            text = document.text
            
            # 只有在命令模式下才提供补全
            if text.startswith("/"):
                cmd_part = text[1:].lower()
                
                for cmd, info in self.commands.items():
                    # 匹配命令名
                    if cmd.startswith(cmd_part):
                        display = f"/{cmd} - {info['desc']}"
                        yield Completion(
                            cmd,
                            start_position=-len(cmd_part),
                            display=display
                        )
                    # 匹配别名
                    for alias in info.get("aliases", []):
                        if alias.startswith(cmd_part):
                            display = f"/{alias} - {info['desc']} (alias for /{cmd})"
                            yield Completion(
                                cmd,  # 补全为完整命令名
                                start_position=-len(cmd_part),
                                display=display
                            )
    
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False
    CommandCompleter = None

# 导入项目核心模块
try:
    from nbot.services.tools import TOOL_DEFINITIONS, execute_tool
    TOOLS_AVAILABLE = True
except ImportError:
    TOOLS_AVAILABLE = False
    TOOL_DEFINITIONS = []


class CCStyleCLI:
    """Claude Code 风格的 CLI - 简化同步版本"""

    # 命令定义
    COMMANDS = {
        "quit": {"desc": "退出CLI", "aliases": ["exit", "q"]},
        "clear": {"desc": "清屏", "aliases": []},
        "model": {"desc": "切换模型", "aliases": []},
        "models": {"desc": "列出所有模型", "aliases": []},
        "tools": {"desc": "列出可用工具", "aliases": []},
        "sessions": {"desc": "列出最近会话", "aliases": []},
        "thinking": {"desc": "切换思考过程显示", "aliases": []},
        "reset": {"desc": "重置当前会话", "aliases": []},
        "help": {"desc": "显示帮助信息", "aliases": ["h", "?"]},
        "memory": {"desc": "查看记忆列表", "aliases": ["mem"]},
        "knowledge": {"desc": "查看知识库", "aliases": ["kb", "know"]},
        "personality": {"desc": "查看/切换人格", "aliases": ["persona", "p"]},
        "tasks": {"desc": "查看定时任务", "aliases": ["task"]},
        "workflows": {"desc": "查看工作流", "aliases": ["flow", "wf"]},
        "config": {"desc": "查看系统配置", "aliases": ["cfg"]},
        "status": {"desc": "查看系统状态", "aliases": ["st"]},
    }

    def __init__(self):
        self.console = Console()
        self.running = True
        self.session_id = None
        self.messages = []
        self.current_model_id = None
        self.available_models = []
        self.show_thinking = True
        self.max_tool_iterations = 10
        self.input_mode = "chat"  # chat, command, help
        self.current_path = os.getcwd()
        self.interrupt_requested = False
        
        # 消息队列
        self.message_queue = []
        self.queue_lock = threading.Lock()
        self.ai_processing = False

        # 确保数据目录存在
        os.makedirs(os.path.join("data", "web"), exist_ok=True)

        # 加载可用模型
        self._load_models()
        
        # 初始化prompt_toolkit session（如果可用）
        self.prompt_session = None
        if PROMPT_TOOLKIT_AVAILABLE:
            try:
                self.prompt_session = PromptSession(
                    completer=CommandCompleter(self.COMMANDS),
                    complete_while_typing=True,  # 输入时实时补全
                )
            except Exception:
                pass

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
                pass

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

    def _get_ascii_art_two_lines(self) -> str:
        """生成两行ASCII艺术字 - Neko在上，Bot在下，使用倾斜实体字体"""
        if PYFIGLET_AVAILABLE:
            # 尝试使用倾斜/实体字体
            for font in ["slant", "lean", "italic", "3-d", "3x5"]:
                try:
                    f = Figlet(font=font)
                    neko = f.renderText("Neko").rstrip()
                    bot = f.renderText("Bot").rstrip()
                    # 检查宽度是否合适
                    neko_max = max(len(line) for line in neko.split('\n'))
                    bot_max = max(len(line) for line in bot.split('\n'))
                    if neko_max <= 35 and bot_max <= 35:
                        return f"{neko}\n{bot}"
                except:
                    continue
        # 备用艺术字 - 倾斜实体风格
        return r"""
    _   _______      __        
   / | / / ___/___  / /______ _
  /  |/ /\__ \/ _ \/ //_/ __ `/
 / /|  /___/ /  __/ ,< / /_/ /
/_/ |_//____/\___/_/|_|\__,_/ 

    ____        __  
   / __ )____  / /__
  / __  / __ \/ //_/
 / /_/ / /_/ / ,<   
/_____/\____/_/|_|  """

    def print_welcome(self):
        """打印欢迎界面"""
        self.console.print()

        # 顶部标题栏
        header = Panel(
            Align.center("[bold cyan]🐱 NekoBot CLI[/bold cyan] [dim]v1.0.0[/dim]"),
            box=HEAVY,
            style="cyan",
            padding=(0, 1)
        )
        self.console.print(header)

        # 主内容区 - 左右分栏（设置固定高度使它们等高）
        left_content = self._get_ascii_art_two_lines()
        left_panel = Panel(
            Align.center(f"[bold cyan]{left_content}[/bold cyan]", vertical="middle"),
            box=ROUNDED,
            style="cyan",
            padding=(0, 0),
            height=20,
            width=40
        )

        # 右侧提示信息
        current_model = self._get_current_model()
        model_name = current_model.get('name', 'Unknown') if current_model else 'Unknown'
        model_provider = current_model.get('provider_type', 'Unknown') if current_model else 'Unknown'
        
        # 转义模型名称中的特殊字符
        model_name = model_name.replace("[", "\\[").replace("]", "\\]")

        tips_text = f"""[bold]Tips for getting started[/bold]

Type a message to chat with AI
Press [yellow]/[/yellow] for command mode
Press [yellow]?[/yellow] for help mode
Press [red]Ctrl+C[/red] to interrupt AI

[dim]Current Model:[/dim] [cyan]{model_name}[/cyan]
[dim]Provider:[/dim] [cyan]{model_provider}[/cyan]
[dim]Tools:[/dim] [cyan]{len(TOOL_DEFINITIONS) if TOOLS_AVAILABLE else 0} available[/cyan]

[dim]Recent activity[/dim]
No recent activity
"""
        right_panel = Panel(
            tips_text,
            box=ROUNDED,
            style="white",
            padding=(1, 2),
            height=20  # 固定高度，与左侧面板相同
        )

        # 创建布局 - 使用外框包裹整个内容
        table = Table(show_header=False, box=None, expand=True)
        table.add_column("left", ratio=1)
        table.add_column("right", ratio=2)
        table.add_row(left_panel, right_panel)
        
        # 外框包裹整个欢迎内容
        content_panel = Panel(
            table,
            box=ROUNDED,
            style="cyan",
            padding=(1, 2),
            title="[bold cyan]🐱 Welcome[/bold cyan]",
            title_align="left"
        )

        self.console.print(content_panel)
        self.console.print()

    def _render_input_box(self, mode: str = "chat") -> str:
        """渲染输入框"""
        if mode == "command":
            return "[bold yellow]/[/bold yellow] [bold]>[/bold]"
        elif mode == "help":
            return "[bold magenta]?[/bold magenta] [bold]>[/bold]"
        else:
            return "[bold cyan]›[/bold cyan]"

    def _get_input(self, mode: str = "chat") -> tuple[str, bool]:
        """
        获取用户输入，支持命令候选
        返回: (输入内容, 是否打断)
        """
        # prompt_toolkit不支持Rich标签，使用纯文本提示符
        if self.prompt_session and mode == "chat":
            prompt = ">"  # 纯文本提示符
        else:
            prompt = self._render_input_box(mode)

        try:
            # 如果有prompt_toolkit，使用它来获取输入（支持实时候选）
            if self.prompt_session and mode == "chat":
                try:
                    user_input = self.prompt_session.prompt(f"{prompt} ").strip()
                except Exception:
                    # 如果prompt_toolkit失败，回退到普通输入
                    user_input = self.console.input(f"{prompt} ").strip()
            else:
                user_input = self.console.input(f"{prompt} ").strip()
            
            # 检测模式切换前缀
            if user_input.startswith("/"):
                self.input_mode = "command"
                cmd_input = user_input[1:].strip()
                # 如果没有输入具体命令，显示所有命令
                if not cmd_input:
                    self._show_command_candidates("")
                    return None, False
                return cmd_input, False
            elif user_input.startswith("?"):
                self.input_mode = "help"
                return user_input[1:].strip(), False

            return user_input, False

        except (KeyboardInterrupt, EOFError):
            return "", True

    def _show_command_candidates(self, partial: str):
        """显示命令候选"""
        # 查找匹配的命令
        matches = []
        search_term = partial.lower() if partial else ""
        
        for cmd, info in self.COMMANDS.items():
            if cmd.startswith(search_term):
                matches.append((cmd, info["desc"]))
            # 检查别名
            for alias in info.get("aliases", []):
                if alias.startswith(search_term):
                    matches.append((f"{cmd} (alias: {alias})", info["desc"]))
        
        # 显示候选
        if not search_term:
            # 没有输入，显示所有命令
            if matches:
                self.console.print()
                self.console.print(f"[bold cyan]Available commands ({len(matches)}):[/bold cyan]")
                # 按命令名排序
                sorted_matches = sorted(matches, key=lambda x: x[0])
                for cmd, desc in sorted_matches:
                    self.console.print(f"  [yellow]/{cmd}[/yellow] - {desc}")
                self.console.print()
        elif len(matches) == 1:
            # 只有一个匹配，直接提示
            cmd, desc = matches[0]
            self.console.print(f"[dim]↳ {desc}[/dim]")
        elif len(matches) > 1:
            # 多个匹配，显示列表
            self.console.print(f"[dim]Candidates ({len(matches)}):[/dim]")
            for cmd, desc in matches:
                self.console.print(f"  [yellow]/{cmd}[/yellow] - {desc}")

    def _render_inline_thinking(self, thinking: str):
        """内联渲染思考过程"""
        if not thinking or not self.show_thinking:
            return
        
        # 使用灰色斜体显示思考过程，与对话融为一体
        thinking_lines = thinking.strip().split('\n')
        for line in thinking_lines:
            if line.strip():
                self.console.print(f"[dim italic]💭 {line}[/dim italic]")

    def _render_inline_tool_call(self, tool_name: str, arguments: Dict):
        """内联渲染工具调用"""
        args_str = json.dumps(arguments, ensure_ascii=False)
        if len(args_str) > 100:
            args_str = args_str[:100] + "..."
        # 转义特殊字符避免Rich标签解析问题
        args_str = args_str.replace("[", r"\[").replace("]", r"\]")
        tool_name = tool_name.replace("[", r"\[").replace("]", r"\]")
        # 使用Text对象避免Rich标签解析问题
        from rich.text import Text
        text = Text()
        text.append("🔧 ", style="dim yellow")
        text.append(f"{tool_name}({args_str})", style="dim yellow")
        self.console.print(text)

    def _render_inline_tool_result(self, result: Dict, success: bool):
        """内联渲染工具结果"""
        icon = "✓" if success else "✗"
        if success:
            self.console.print(f"[dim green]{icon} Done[/dim]")
        else:
            self.console.print(f"[dim red]{icon} Failed[/dim]")

    def _call_ai(self, messages: List[Dict]) -> Dict[str, Any]:
        """调用AI，支持工具调用和多轮思考"""
        model = self._get_current_model()
        if not model:
            return {"content": "error: no model available", "thinking": "", "tool_calls": []}

        try:
            api_key = model.get("api_key", "")
            base_url = model.get("base_url", "")
            model_name = model.get("model", "")
            supports_tools = model.get("supports_tools", True)

            if not api_key or not base_url:
                return {"content": "error: model not configured", "thinking": "", "tool_calls": []}

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

            tools = self._get_tool_definitions() if supports_tools else []
            tool_messages = copy.deepcopy(messages)
            all_thinking = []
            all_tool_calls = []

            for iteration in range(self.max_tool_iterations):
                # 检查是否被打断
                if self.interrupt_requested:
                    return {
                        "content": "[interrupted by user]",
                        "thinking": "\n\n".join(all_thinking),
                        "tool_calls": all_tool_calls,
                        "iterations": iteration + 1,
                        "interrupted": True
                    }

                payload = {
                    "model": model_name,
                    "messages": tool_messages,
                    "temperature": model.get("temperature", 0.7),
                    "max_tokens": model.get("max_tokens", 2000),
                    "stream": False
                }

                if tools and supports_tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"

                response = requests.post(url, headers=headers, json=payload, timeout=120)
                response.raise_for_status()
                data = response.json()

                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})

                # 提取并显示思考内容
                thinking = message.get("reasoning_content", "") or message.get("thinking", "")
                if thinking:
                    all_thinking.append(f"Round {iteration + 1}: {thinking}")
                    # 内联显示思考过程
                    self._render_inline_thinking(thinking)

                # 检查是否有工具调用
                if message.get("tool_calls") and supports_tools:
                    tool_calls = message["tool_calls"]
                    all_tool_calls.extend(tool_calls)

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
                        # 检查打断
                        if self.interrupt_requested:
                            return {
                                "content": "[interrupted by user]",
                                "thinking": "\n\n".join(all_thinking),
                                "tool_calls": all_tool_calls,
                                "iterations": iteration + 1,
                                "interrupted": True
                            }

                        tool_name = tool_call.get("function", {}).get("name")
                        try:
                            arguments = json.loads(tool_call.get("function", {}).get("arguments", "{}"))
                        except:
                            arguments = {}

                        # 内联显示工具调用
                        self._render_inline_tool_call(tool_name, arguments)

                        # 执行工具
                        result = self._execute_tool(tool_name, arguments)
                        success = result.get("success", False)

                        # 内联显示工具结果
                        self._render_inline_tool_result(result, success)

                        tool_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.get("id"),
                            "content": json.dumps(result, ensure_ascii=False)
                        })
                else:
                    # 没有工具调用，得到最终回复
                    final_content = message.get("content", "")
                    if not final_content and "base_resp" in data:
                        final_content = data.get("reply", "")

                    return {
                        "content": final_content,
                        "thinking": "\n\n".join(all_thinking),
                        "tool_calls": all_tool_calls,
                        "iterations": iteration + 1,
                        "interrupted": False
                    }

            return {
                "content": "tool iterations exceeded limit",
                "thinking": "\n\n".join(all_thinking),
                "tool_calls": all_tool_calls,
                "iterations": self.max_tool_iterations,
                "interrupted": False
            }

        except requests.exceptions.RequestException as e:
            return {"content": f"network error: {str(e)}", "thinking": "", "tool_calls": [], "interrupted": False}
        except Exception as e:
            return {"content": f"error: {str(e)}", "thinking": "", "tool_calls": [], "interrupted": False}

    def _get_tool_definitions(self) -> List[Dict]:
        """获取工具定义列表"""
        if not TOOLS_AVAILABLE:
            return []
        return TOOL_DEFINITIONS

    def _execute_tool(self, tool_name: str, arguments: Dict) -> Dict:
        """执行工具"""
        if not TOOLS_AVAILABLE:
            return {"success": False, "error": "tools not available"}
        try:
            tool_context = {"session_id": self.session_id} if self.session_id else {}
            result = execute_tool(tool_name, arguments, tool_context)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}

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

            session_name = "CLI Session"
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
                "messages": [
                    {
                        "id": str(i),
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", ""),
                        "timestamp": msg.get("timestamp", datetime.now().isoformat()),
                        "sender": "user" if msg.get("role") == "user" else "AI",
                        "source": "cli",
                        "session_id": self.session_id,
                    }
                    for i, msg in enumerate(self.messages)
                ],
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }

            sessions[self.session_id] = session_data
            os.makedirs(os.path.dirname(sessions_file), exist_ok=True)
            with open(sessions_file, 'w', encoding='utf-8') as f:
                json.dump(sessions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            pass

    def _handle_command(self, cmd: str):
        """处理命令模式"""
        cmd = cmd.strip().lower()

        if cmd in ["quit", "exit", "q"]:
            self.running = False
            return True
        elif cmd == "clear":
            self.console.clear()
            self.print_welcome()
            return True
        elif cmd == "model":
            self._show_model_switcher()
            return True
        elif cmd == "models":
            self._show_models()
            return True
        elif cmd == "tools":
            self._show_tools()
            return True
        elif cmd == "sessions":
            self._show_sessions()
            return True
        elif cmd == "thinking":
            self.show_thinking = not self.show_thinking
            status = "on" if self.show_thinking else "off"
            self.console.print(f"[dim]thinking {status}[/dim]")
            return True
        elif cmd == "reset":
            self.messages = []
            self.session_id = str(uuid.uuid4())
            self.console.print("[dim]session reset[/dim]")
            return True
        elif cmd == "memory" or cmd == "mem":
            self._show_memory()
            return True
        elif cmd == "knowledge" or cmd == "kb" or cmd == "know":
            self._show_knowledge()
            return True
        elif cmd == "personality" or cmd == "persona" or cmd == "p":
            self._show_personality()
            return True
        elif cmd == "tasks" or cmd == "task":
            self._show_tasks()
            return True
        elif cmd == "workflows" or cmd == "flow" or cmd == "wf":
            self._show_workflows()
            return True
        elif cmd == "config" or cmd == "cfg":
            self._show_config()
            return True
        elif cmd == "status" or cmd == "st":
            self._show_status()
            return True
        elif cmd.startswith("model "):
            model_name = cmd[6:].strip()
            self._switch_model_by_name(model_name)
            return True
        else:
            self.console.print(f"[red]unknown command: {cmd}[/red]")
            self.console.print("[dim]type /help for available commands[/dim]")
            return True

    def _handle_help(self, topic: str = ""):
        """处理帮助模式"""
        topic = topic.strip().lower()

        if not topic or topic == "help":
            help_text = """
[bold cyan]NekoBot CLI Help[/bold cyan]

[bold]Input Modes:[/bold]
  [cyan]›[/cyan] normal chat mode
  [yellow]/[/yellow] command mode
  [magenta]?[/magenta] help mode

[bold]Commands (/):[/bold]
  /quit, /exit    Exit CLI
  /clear          Clear screen
  /model          Switch model
  /models         List all models
  /tools          List available tools
  /sessions       List recent sessions
  /thinking       Toggle thinking display
  /reset          Reset current session
  /help           Show this help

[bold]Shortcuts:[/bold]
  Ctrl+C          Interrupt AI / Exit
  ↑/↓             Navigate history

[bold]Tips:[/bold]
  Start with / for commands
  Start with ? for help
  Just type to chat with AI
  Press Ctrl+C during AI response to interrupt
  Type partial command to see candidates
"""
            self.console.print(help_text)
        elif topic == "commands":
            self._show_command_help()
        elif topic == "tools":
            self._show_tools()
        elif topic == "models":
            self._show_models()
        else:
            self.console.print(f"[red]no help available for: {topic}[/red]")

        return True

    def _show_command_help(self):
        """显示命令帮助"""
        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("Command", style="yellow", width=20)
        table.add_column("Description", style="white")

        commands = [
            ("quit, exit, q", "Exit the CLI"),
            ("clear", "Clear the screen"),
            ("model", "Switch to another model"),
            ("models", "List all available models"),
            ("tools", "List available tools"),
            ("sessions", "List recent sessions"),
            ("thinking", "Toggle thinking display"),
            ("reset", "Reset current session"),
            ("model <name>", "Switch to specific model"),
        ]

        for cmd, desc in commands:
            table.add_row(cmd, desc)

        self.console.print(table)

    def _show_model_switcher(self):
        """显示模型切换器"""
        if not self.available_models:
            self.console.print("[red]no models available[/red]")
            return

        self.console.print("\n[bold cyan]Available Models:[/bold cyan]\n")

        for i, model in enumerate(self.available_models, 1):
            marker = "●" if model.get("id") == self.current_model_id else "○"
            name = model.get('name', 'Unknown')
            provider = model.get('provider_type', 'Unknown')
            self.console.print(f"  {marker} {i}. {name} ([dim]{provider}[/dim])")

        self.console.print()
        choice = self.console.input("[cyan]Select model number (or name):[/cyan] ").strip()

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(self.available_models):
                model = self.available_models[idx]
                self._set_active_model(model.get("id"))
                model_name = model.get('name', '').replace("[", "\\[").replace("]", "\\]")
                self.console.print(f"[green]✓ switched to {model_name}[/green]")
        else:
            self._switch_model_by_name(choice)

    def _switch_model_by_name(self, name: str):
        """按名称切换模型"""
        name = name.lower()
        for model in self.available_models:
            if name in model.get('name', '').lower() or name in model.get('model', '').lower():
                self._set_active_model(model.get("id"))
                model_name = model.get('name', '').replace("[", "\\[").replace("]", "\\]")
                self.console.print(f"[green]✓ switched to {model_name}[/green]")
                return
        self.console.print(f"[red]model not found: {name}[/red]")

    def _show_models(self):
        """显示所有模型"""
        if not self.available_models:
            self.console.print("[red]no models available[/red]")
            return

        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("#", style="dim", width=4)
        table.add_column("Name", style="cyan")
        table.add_column("Model", style="white")
        table.add_column("Provider", style="blue")
        table.add_column("Tools", style="green", width=6)
        table.add_column("Active", style="yellow", width=8)

        for i, model in enumerate(self.available_models, 1):
            name = model.get('name', 'Unknown')
            model_name = model.get('model', 'Unknown')
            provider = model.get('provider_type', 'Unknown')
            tools = "✓" if model.get('supports_tools') else "✗"
            active = "●" if model.get('id') == self.current_model_id else ""

            table.add_row(str(i), name, model_name, provider, tools, active)

        self.console.print(table)

    def load_tools(self) -> list:
        """加载工具列表"""
        tools_file = os.path.join("data", "web", "tools.json")
        if os.path.exists(tools_file):
            try:
                with open(tools_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return []

    def _show_tools(self):
        """显示可用工具"""
        if not TOOLS_AVAILABLE or not TOOL_DEFINITIONS:
            self.console.print("[dim]no tools available[/dim]")
            return

        self.console.print(f"\n[bold cyan]Available Tools ({len(TOOL_DEFINITIONS)}):[/bold cyan]\n")

        for tool in TOOL_DEFINITIONS:
            func = tool.get("function", {})
            name = func.get("name", "Unknown")
            desc = func.get("description", "")
            self.console.print(f"  [bold]{name}[/bold] - {desc}")

        self.console.print()

    def load_sessions(self) -> list:
        """加载会话列表"""
        sessions_file = os.path.join("data", "web", "sessions.json")
        if not os.path.exists(sessions_file):
            return []
        try:
            with open(sessions_file, 'r', encoding='utf-8') as f:
                sessions_data = json.load(f)
            return list(sessions_data.values())
        except:
            return []

    def _show_sessions(self):
        """显示会话列表"""
        sessions = self.load_sessions()[:10]  # 只显示最近10个

        if not sessions:
            self.console.print("[dim]no sessions[/dim]")
            return

        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("ID", style="dim", width=10)
        table.add_column("Name", style="cyan")
        table.add_column("Messages", style="green", width=10)
        table.add_column("Updated", style="dim", width=16)

        for session in sessions:
            sid = session.get("id", "")[:8]
            name = session.get("name", "Untitled")
            count = len(session.get("messages", []))
            updated = session.get("updated_at", "")

            if updated:
                try:
                    dt = datetime.fromisoformat(updated)
                    updated = dt.strftime("%m-%d %H:%M")
                except:
                    pass

            table.add_row(sid, name, str(count), updated)

        self.console.print(table)

    def _show_memory(self):
        """显示记忆列表"""
        memory_file = os.path.join("data", "web", "memories.json")
        if not os.path.exists(memory_file):
            self.console.print("[dim]no memories[/dim]")
            return

        try:
            with open(memory_file, 'r', encoding='utf-8') as f:
                memories = json.load(f)

            if not memories:
                self.console.print("[dim]no memories[/dim]")
                return

            table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
            table.add_column("ID", style="dim", width=8)
            table.add_column("Title", style="cyan")
            table.add_column("Type", style="blue", width=8)
            table.add_column("Target", style="green", width=10)

            for mem in memories[:20]:
                mid = mem.get("id", "")[:6]
                title = mem.get("title", mem.get("key", "Untitled"))
                mem_type = mem.get("type", "long")
                target = mem.get("target_id", "global")[:8]
                table.add_row(mid, title, mem_type, target)

            self.console.print(table)
            self.console.print(f"\n[dim]Total: {len(memories)} memories[/dim]")
        except Exception as e:
            self.console.print(f"[red]error loading memories: {e}[/red]")

    def _show_knowledge(self):
        """显示知识库"""
        kb_file = os.path.join("data", "knowledge", "knowledge_base.json")
        if not os.path.exists(kb_file):
            self.console.print("[dim]no knowledge base[/dim]")
            return

        try:
            with open(kb_file, 'r', encoding='utf-8') as f:
                kb_data = json.load(f)

            docs = kb_data.get("documents", [])
            if not docs:
                self.console.print("[dim]no documents in knowledge base[/dim]")
                return

            table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
            table.add_column("ID", style="dim", width=8)
            table.add_column("Title", style="cyan")
            table.add_column("Source", style="blue")
            table.add_column("Size", style="green", width=8)

            for doc in docs[:20]:
                did = doc.get("id", "")[:6]
                title = doc.get("title", "Untitled")
                source = doc.get("source", "unknown")
                size = len(doc.get("content", ""))
                table.add_row(did, title, source, f"{size} chars")

            self.console.print(table)
            self.console.print(f"\n[dim]Total: {len(docs)} documents[/dim]")
        except Exception as e:
            self.console.print(f"[red]error loading knowledge: {e}[/red]")

    def _show_personality(self):
        """显示人格配置"""
        personality_file = os.path.join("data", "web", "personality.json")
        if not os.path.exists(personality_file):
            self.console.print("[dim]no personality config[/dim]")
            return

        try:
            with open(personality_file, 'r', encoding='utf-8') as f:
                personality = json.load(f)

            name = personality.get("name", "Unknown")
            prompt = personality.get("prompt", "")

            self.console.print(f"[bold cyan]Personality: {name}[/bold cyan]\n")
            if prompt:
                # 截断过长的prompt
                if len(prompt) > 500:
                    prompt = prompt[:500] + "\n... (truncated)"
                self.console.print(prompt)
            else:
                self.console.print("[dim]no prompt configured[/dim]")
        except Exception as e:
            self.console.print(f"[red]error loading personality: {e}[/red]")

    def _show_tasks(self):
        """显示定时任务"""
        tasks_file = os.path.join("data", "web", "scheduled_tasks.json")
        if not os.path.exists(tasks_file):
            self.console.print("[dim]no scheduled tasks[/dim]")
            return

        try:
            with open(tasks_file, 'r', encoding='utf-8') as f:
                tasks = json.load(f)

            if not tasks:
                self.console.print("[dim]no scheduled tasks[/dim]")
                return

            table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
            table.add_column("ID", style="dim", width=8)
            table.add_column("Name", style="cyan")
            table.add_column("Trigger", style="blue", width=10)
            table.add_column("Enabled", style="green", width=8)

            for task in tasks[:20]:
                tid = task.get("id", "")[:6]
                name = task.get("name", "Untitled")
                trigger = task.get("trigger", "manual")
                enabled = "✓" if task.get("enabled", True) else "✗"
                table.add_row(tid, name, trigger, enabled)

            self.console.print(table)
            self.console.print(f"\n[dim]Total: {len(tasks)} tasks[/dim]")
        except Exception as e:
            self.console.print(f"[red]error loading tasks: {e}[/red]")

    def _show_workflows(self):
        """显示工作流"""
        workflows_file = os.path.join("data", "web", "workflows.json")
        if not os.path.exists(workflows_file):
            self.console.print("[dim]no workflows[/dim]")
            return

        try:
            with open(workflows_file, 'r', encoding='utf-8') as f:
                workflows = json.load(f)

            if not workflows:
                self.console.print("[dim]no workflows[/dim]")
                return

            table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
            table.add_column("ID", style="dim", width=8)
            table.add_column("Name", style="cyan")
            table.add_column("Trigger", style="blue", width=10)
            table.add_column("Enabled", style="green", width=8)

            for wf in workflows[:20]:
                wid = wf.get("id", "")[:6]
                name = wf.get("name", "Untitled")
                trigger = wf.get("trigger", "manual")
                enabled = "✓" if wf.get("enabled", True) else "✗"
                table.add_row(wid, name, trigger, enabled)

            self.console.print(table)
            self.console.print(f"\n[dim]Total: {len(workflows)} workflows[/dim]")
        except Exception as e:
            self.console.print(f"[red]error loading workflows: {e}[/red]")

    def _show_config(self):
        """显示系统配置"""
        config_file = os.path.join("data", "web", "settings.json")
        if not os.path.exists(config_file):
            self.console.print("[dim]no config file[/dim]")
            return

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)

            table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
            table.add_column("Key", style="cyan")
            table.add_column("Value", style="white")

            for key, value in config.items():
                value_str = str(value)
                if len(value_str) > 50:
                    value_str = value_str[:50] + "..."
                table.add_row(key, value_str)

            self.console.print(table)
        except Exception as e:
            self.console.print(f"[red]error loading config: {e}[/red]")

    def _show_status(self):
        """显示系统状态"""
        self.console.print("[bold cyan]System Status[/bold cyan]\n")

        # 模型状态
        current_model = self._get_current_model()
        if current_model:
            self.console.print(f"[green]●[/green] Model: {current_model.get('name', 'Unknown')}")
        else:
            self.console.print(f"[red]●[/red] Model: Not configured")

        # 工具状态
        if TOOLS_AVAILABLE:
            self.console.print(f"[green]●[/green] Tools: {len(TOOL_DEFINITIONS)} available")
        else:
            self.console.print(f"[red]●[/red] Tools: Not available")

        # 会话数量
        sessions_file = os.path.join("data", "web", "sessions.json")
        if os.path.exists(sessions_file):
            try:
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions = json.load(f)
                self.console.print(f"[green]●[/green] Sessions: {len(sessions)}")
            except:
                self.console.print(f"[dim]●[/dim] Sessions: Unknown")

        # 记忆数量
        memory_file = os.path.join("data", "web", "memories.json")
        if os.path.exists(memory_file):
            try:
                with open(memory_file, 'r', encoding='utf-8') as f:
                    memories = json.load(f)
                self.console.print(f"[green]●[/green] Memories: {len(memories)}")
            except:
                pass

        # 知识库数量
        kb_file = os.path.join("data", "knowledge", "knowledge_base.json")
        if os.path.exists(kb_file):
            try:
                with open(kb_file, 'r', encoding='utf-8') as f:
                    kb_data = json.load(f)
                docs = kb_data.get("documents", [])
                self.console.print(f"[green]●[/green] Knowledge: {len(docs)} documents")
            except:
                pass

        self.console.print()

    def _print_footer(self):
        """打印底部状态栏"""
        current_model = self._get_current_model()
        model_name = current_model.get('name', 'Unknown') if current_model else 'Unknown'
        
        # 转义模型名称中的特殊字符，避免Rich标签解析错误
        model_name = model_name.replace("[", "\\[").replace("]", "\\]")

        footer_text = f"[dim]? for shortcuts[/dim]"
        footer_text += f"  [cyan]● {model_name}[/cyan]"

        if TOOLS_AVAILABLE:
            footer_text += f"  [green]● {len(TOOL_DEFINITIONS)} tools[/green]"
        
        # 显示队列中的消息数
        with self.queue_lock:
            if self.message_queue:
                footer_text += f"  [yellow]● {len(self.message_queue)} queued[/yellow]"

        self.console.print()
        self.console.print(footer_text)

    def _render_markdown(self, content: str):
        """渲染Markdown内容，优化列表展示"""
        if not content:
            return
        
        # 使用自定义Markdown选项优化列表显示
        from rich.markdown import Markdown
        md = Markdown(
            content, 
            code_theme="monokai",
            justify="left"
        )
        self.console.print(md)

    def run(self):
        """运行CLI - 直接进入输入模式"""
        # 初始化会话
        if not self.session_id:
            self.session_id = str(uuid.uuid4())

        # 清屏并显示欢迎界面
        self.console.clear()
        self.print_welcome()

        # 主循环 - 同步处理
        while self.running:
            try:
                # 检查队列中是否有待处理的消息
                with self.queue_lock:
                    if self.message_queue and not self.ai_processing:
                        # 处理队列中的消息
                        msg = self.message_queue.pop(0)
                        self._process_chat_message(msg)
                        continue

                # 打印底部状态
                self._print_footer()

                # 获取输入
                user_input, interrupted = self._get_input(self.input_mode)

                if interrupted:
                    self.running = False
                    break

                if user_input is None:
                    # 空命令，已显示候选，继续等待输入
                    continue
                
                if not user_input:
                    continue

                # 根据模式处理输入
                if self.input_mode == "command":
                    handled = self._handle_command(user_input)
                    self.input_mode = "chat"
                    if not handled:
                        continue

                elif self.input_mode == "help":
                    handled = self._handle_help(user_input)
                    self.input_mode = "chat"
                    if not handled:
                        continue

                else:
                    # 聊天模式
                    if user_input.startswith("/"):
                        self._handle_command(user_input[1:])
                        continue

                    # 检查AI是否正在处理
                    with self.queue_lock:
                        if self.ai_processing:
                            # AI正在处理，将消息加入队列
                            self.message_queue.append(user_input)
                            self.console.print(f"[dim][message queued: {len(self.message_queue)}][/dim]")
                            continue

                    # AI未在处理，直接处理消息
                    self._process_chat_message(user_input)

            except KeyboardInterrupt:
                if self.ai_processing and not self.interrupt_requested:
                    # 请求打断当前AI响应
                    self.interrupt_requested = True
                    self.console.print("\n[dim][interrupting...][/dim]")
                else:
                    self.running = False
                    break
            except Exception as e:
                self.console.print(f"[red]error: {e}[/red]")

        # 清理
        self.save_session()
        self.console.print("\n[dim]Goodbye! 🐱[/dim]")

    def _process_chat_message(self, message: str):
        """处理聊天消息（同步版本）"""
        # 设置AI处理状态
        with self.queue_lock:
            self.ai_processing = True
        
        # 重置打断标志
        self.interrupt_requested = False

        # 添加用户消息到历史（不显示）
        self.messages.append({
            "role": "user",
            "content": message,
            "timestamp": datetime.now().isoformat()
        })

        # 显示AI正在思考
        self.console.print(f"[bold cyan]🐱 NekoBot[/bold cyan] [dim]thinking...[/dim]")

        # 调用AI（内联显示思考过程）
        result = self._call_ai(self.messages)

        content = result.get("content", "")
        interrupted = result.get("interrupted", False)

        # 添加AI消息
        self.messages.append({
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })

        # 显示AI回复（如果未被打断）
        if not interrupted:
            self.console.print()
            self._render_markdown(content)
        else:
            self.console.print(f"\n[dim][interrupted][/dim]")

        self.console.print()

        # 自动保存
        self.save_session()
        
        # 重置AI处理状态
        with self.queue_lock:
            self.ai_processing = False


def main():
    """CLI入口"""
    cli = CCStyleCLI()
    cli.run()


if __name__ == "__main__":
    main()
