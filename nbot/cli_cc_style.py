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
import shutil
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


def escape_rich_tags(text: str) -> str:
    """转义Rich标签，防止解析错误
    将 [ 替换为 &#91;，] 替换为 &#93; (HTML实体编码)
    这样Rich不会解析它们，但显示时看起来还是方括号
    """
    if not isinstance(text, str):
        text = str(text)
    # 使用HTML实体编码，Rich不会解析，但显示效果相同
    return text.replace("[", "&#91;").replace("]", "&#93;")

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
    from prompt_toolkit.formatted_text import ANSI
    
    class CommandCompleter(Completer):
        """命令补全器 - 用于prompt_toolkit"""
        
        def __init__(self, commands, workspace_root=None):
            self.commands = commands
            self.workspace_root = workspace_root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        def get_completions(self, document, complete_event):
            text = document.text
            
            if text.startswith("@"):
                for completion in self._get_file_completions(text):
                    yield completion
            elif text.startswith("/"):
                cmd_part = text[1:].lower()
                
                for cmd, info in self.commands.items():
                    if cmd.startswith(cmd_part):
                        display = f"/{cmd} - {info['desc']}"
                        yield Completion(
                            cmd,
                            start_position=-len(cmd_part),
                            display=display
                        )
                    for alias in info.get("aliases", []):
                        if alias.startswith(cmd_part):
                            display = f"/{alias} - {info['desc']} (alias for /{cmd})"
                            yield Completion(
                                cmd,
                                start_position=-len(cmd_part),
                                display=display
                            )
        
        def _get_file_completions(self, text):
            """获取文件路径补全"""
            import glob
            
            file_part = text[1:]
            
            if '/' in file_part or '\\' in file_part:
                search_dir = os.path.dirname(file_part) if os.path.dirname(file_part) else "."
                prefix = os.path.basename(file_part)
                full_dir = os.path.join(self.workspace_root, search_dir) if not os.path.isabs(search_dir) else search_dir
            else:
                search_dir = "."
                prefix = file_part
                full_dir = self.workspace_root
            
            if not os.path.isdir(full_dir):
                return
            
            try:
                if prefix:
                    pattern = os.path.join(full_dir, prefix + "*")
                else:
                    pattern = os.path.join(full_dir, "*")
                
                for path in glob.glob(pattern):
                    if os.path.isfile(path):
                        name = os.path.basename(path)
                        rel_path = os.path.relpath(path, self.workspace_root)
                        display = f"📄 {rel_path}"
                        yield Completion(
                            name,
                            start_position=-len(prefix),
                            display=display
                        )
            except Exception:
                pass
    
    PROMPT_TOOLKIT_AVAILABLE = True
except ImportError:
    PROMPT_TOOLKIT_AVAILABLE = False
    CommandCompleter = None

# 导入项目核心模块
try:
    from nbot.services.tools import TOOL_DEFINITIONS, WORKSPACE_TOOL_DEFINITIONS, execute_tool
    TOOLS_AVAILABLE = True
except ImportError:
    TOOLS_AVAILABLE = False
    TOOL_DEFINITIONS = []
    WORKSPACE_TOOL_DEFINITIONS = []

# 尝试导入工具注册表
try:
    from nbot.services.tool_registry import get_all_tool_definitions as get_registered_tools
    TOOL_REGISTRY_AVAILABLE = True
except ImportError:
    TOOL_REGISTRY_AVAILABLE = False
    def get_registered_tools():
        return []


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
        "new": {"desc": "创建新会话", "aliases": ["n"]},
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
                workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                self.prompt_session = PromptSession(
                    completer=CommandCompleter(self.COMMANDS, workspace_root),
                    complete_while_typing=True,
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
        model_name = escape_rich_tags(model_name)
        model_provider = escape_rich_tags(model_provider)

        tips_text = f"""[bold]Tips for getting started[/bold]

Type a message to chat with AI
Press [yellow]/[/yellow] for command mode
Press [yellow]?[/yellow] for help mode
Press [red]Ctrl+C[/red] to interrupt AI

[dim]Current Model:[/dim] [cyan]{model_name}[/cyan]
[dim]Provider:[/dim] [cyan]{model_provider}[/cyan]
[dim]Tools:[/dim] [cyan]{len(self._get_all_tool_names())} available[/cyan]

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
        """渲染输入框 - 带动态效果"""
        is_processing = False
        try:
            with self.queue_lock:
                is_processing = self.ai_processing
        except:
            pass
        
        if is_processing:
            return "\033[2m⋯\033[0m"
        
        if mode == "command":
            return "\033[1;33m/\033[0m"
        elif mode == "help":
            return "\033[1;35m?\033[0m"
        else:
            msg_count = len(self.messages)
            if msg_count == 0:
                return "\033[1;36m›\033[0m"
            elif msg_count < 10:
                return "\033[1;32m›\033[0m"
            elif msg_count < 20:
                return "\033[1;33m›\033[0m"
            else:
                return "\033[1;35m›\033[0m"

    def _process_file_references(self, message: str) -> tuple[str, list]:
        """处理消息中的 @filepath 引用，返回 (处理后的消息, 附件列表)"""
        import re
        
        attachments = []
        processed_message = message
        
        workspace_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        
        pattern = r'@([^\s@]+)'
        matches = list(re.finditer(pattern, message))
        
        for match in matches:
            filepath = match.group(1)
            start, end = match.span()
            
            resolved_path = self._resolve_file_path(filepath, workspace_root)
            
            if resolved_path and os.path.isfile(resolved_path):
                try:
                    file_size = os.path.getsize(resolved_path)
                    max_size = 100 * 1024
                    
                    if file_size > max_size:
                        self.console.print(f"[yellow]文件过大，跳过: {filepath} ({file_size} bytes)[/yellow]")
                        continue
                    
                    with open(resolved_path, 'r', encoding='utf-8') as f:
                        file_content = f.read()
                    
                    rel_path = os.path.relpath(resolved_path, workspace_root)
                    attachment = {
                        "type": "file",
                        "path": resolved_path,
                        "relative_path": rel_path,
                        "content": file_content,
                        "source": filepath
                    }
                    attachments.append(attachment)
                    
                    file_info = f"[文件: {rel_path}]\n```\n{file_content}\n```"
                    processed_message = processed_message[:start] + file_info + processed_message[end:]
                    
                    attachments[-1]["content"] = file_info
                    attachments[-1]["source"] = match.group(0)
                    
                except Exception as e:
                    self.console.print(f"[red]读取文件失败: {filepath} - {str(e)}[/red]")
            else:
                self.console.print(f"[yellow]文件不存在: {filepath}[/yellow]")
        
        return processed_message, attachments

    def _resolve_file_path(self, filepath: str, workspace_root: str) -> str:
        """解析文件路径，支持绝对路径、相对路径"""
        filepath = filepath.strip()
        
        if os.path.isabs(filepath):
            return filepath
        
        if filepath.startswith('.'):
            return os.path.abspath(filepath)
        
        return os.path.join(workspace_root, filepath)

    def _display_user_message(self, content: str, attachments: list = None):
        """显示用户消息 - 带上下边框"""
        from rich.text import Text
        
        line_char = "\u2500"
        width = shutil.get_terminal_size().columns
        
        # 使用 Rich 渲染边框
        self.console.print(f"[dim]{line_char * width}[/dim]")
        
        # 渲染用户消息
        text = Text()
        text.append("> ", style="bold green")
        text.append(content)
        self.console.print(text)
        
        # 渲染附件
        if attachments:
            for att in attachments:
                if att.get("type") == "file":
                    self.console.print(f"[dim]📎 {att.get('relative_path', att.get('source'))}[/dim]")
        
        # 渲染底部边框
        self.console.print(f"[dim]{line_char * width}[/dim]")

    def _get_input(self) -> tuple[str, bool]:
        """
        获取用户输入，支持命令候选和动态效果
        返回: (输入内容, 是否打断)
        """
        self._show_input_hint()
        
        prompt = self._render_input_box("chat")
        line_char = "\u2500"
        width = shutil.get_terminal_size().columns
        top_line = f"\033[90m{line_char * width}\033[0m\n"
        bottom_line = f"\033[90m{line_char * width}\033[0m\n"

        sys.stdout.write(top_line)
        sys.stdout.flush()

        try:
            if self.prompt_session:
                try:
                    user_input = self.prompt_session.prompt(ANSI(f"{prompt} ")).strip()
                except Exception:
                    user_input = self.console.input(f"{prompt} ").strip()
            else:
                user_input = self.console.input(f"{prompt} ").strip()

            sys.stdout.write(bottom_line)
            sys.stdout.flush()
            return user_input, False

        except (KeyboardInterrupt, EOFError):
            sys.stdout.write(bottom_line)
            sys.stdout.flush()
            return "", True

    def _show_input_hint(self):
        """显示输入提示 - 根据当前状态动态变化"""
        from rich.text import Text
        
        # 获取当前模型和人格
        current_model = self._get_current_model()
        model_name = current_model.get('name', 'Unknown') if current_model else 'Unknown'
        
        current_personality = self._get_current_personality()
        personality_name = current_personality.get('name', 'Unknown')
        
        # 获取消息数量
        msg_count = len(self.messages)
        
        # 构建动态提示
        text = Text()
        
        # 根据消息数量显示不同的提示
        if msg_count == 0:
            text.append("💬 ", style="dim")
            text.append(f"Start chatting with {escape_rich_tags(personality_name)}", style="dim cyan")
        elif msg_count < 5:
            text.append("🐱 ", style="dim")
            text.append("Keep the conversation going", style="dim green")
        elif msg_count < 15:
            text.append("✨ ", style="dim")
            text.append(f"{msg_count} messages so far", style="dim yellow")
        else:
            text.append("📝 ", style="dim")
            text.append(f"Long conversation ({msg_count} msgs)", style="dim magenta")
        
        # 显示模型信息
        text.append(f"  [", style="dim")
        text.append(escape_rich_tags(model_name[:15]), style="dim cyan")
        text.append("]", style="dim")
        
        self.console.print(text)

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
                    cmd = escape_rich_tags(cmd)
                    desc = escape_rich_tags(desc)
                    self.console.print(f"  [yellow]/{cmd}[/yellow] - {desc}")
                self.console.print()
        elif len(matches) == 1:
            # 只有一个匹配，直接提示
            cmd, desc = matches[0]
            desc = escape_rich_tags(desc)
            self.console.print(f"[dim]↳ {desc}[/dim]")
        elif len(matches) > 1:
            # 多个匹配，显示列表
            self.console.print(f"[dim]Candidates ({len(matches)}):[/dim]")
            for cmd, desc in matches:
                cmd = escape_rich_tags(cmd)
                desc = escape_rich_tags(desc)
                self.console.print(f"  [yellow]/{cmd}[/yellow] - {desc}")

    def _render_inline_thinking(self, thinking: str):
        """内联渲染思考过程"""
        if not thinking or not self.show_thinking:
            return
        
        from rich.text import Text
        thinking_lines = [line for line in thinking.strip().split('\n') if line.strip()]
        for i, line in enumerate(thinking_lines):
            line = escape_rich_tags(line)
            text = Text()
            text.append("💭 ", style="dim italic")
            text.append(line, style="dim italic")
            self.console.print(text)

    def _render_tool_call_and_result(self, tool_name: str, arguments: Dict, result: Dict, success: bool):
        """以卡片样式渲染工具调用和结果"""
        from rich.text import Text
        from rich.panel import Panel
        from rich.console import Group
        
        # 工具名称不截断，完整显示
        tool_name_display = escape_rich_tags(tool_name)
        
        # 准备参数字符串，限制长度
        args_str = json.dumps(arguments, ensure_ascii=False)
        args_str = escape_rich_tags(args_str)
        # 限制参数显示长度为30字符
        if len(args_str) > 30:
            args_str = args_str[:27] + "..."
        
        # 构建卡片内容
        card_lines = []
        
        # 工具名称行
        name_line = Text()
        name_line.append("🔧 ", style="yellow")
        name_line.append(tool_name_display, style="bold cyan")
        card_lines.append(name_line)
        
        # 参数行（如果有参数）
        if args_str and args_str != "{}":
            args_line = Text(f"   {args_str}", style="dim")
            card_lines.append(args_line)
        
        # 结果行
        result_line = Text()
        if success:
            # 特殊处理 workspace_send_file，显示可点击的文件链接
            if tool_name == "workspace_send_file" and result.get("file_url"):
                file_url = result.get("file_url")
                filename = result.get("filename", "文件")
                size_str = result.get("size_str", "")
                
                # 截断文件名显示
                if len(filename) > 25:
                    filename = filename[:22] + "..."
                
                result_line.append("   ✓ ", style="green")
                # 创建可点击的文件链接
                link_text = Text(filename, style="blue underline")
                link_text.stylize(f"link {file_url}")
                result_line.append(link_text)
                if size_str:
                    result_line.append(f" {size_str}", style="dim")
                result_line.append(" 📄", style="dim")
            else:
                result_line.append("   ✓ ", style="green")
                result_line.append("Success", style="green")
        else:
            error_msg = result.get("error", "")
            if error_msg:
                error_msg = escape_rich_tags(error_msg)
                # 截断错误信息
                if len(error_msg) > 30:
                    error_msg = error_msg[:27] + "..."
                result_line.append("   ✗ ", style="red")
                result_line.append(error_msg, style="red")
            else:
                result_line.append("   ✗ ", style="red")
                result_line.append("Failed", style="red")
        
        card_lines.append(result_line)
        
        # 创建卡片面板，使用紧凑样式
        card_content = Group(*card_lines)
        card = Panel(
            card_content,
            border_style="dim" if success else "red",
            padding=(0, 1),
            width=50,  # 固定卡片宽度
            expand=False,
            box=box.ROUNDED
        )
        
        self.console.print(card)

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
                    "stream": True  # 启用流式输出
                }
                
                # 如果配置了 max_tokens，则使用配置值
                max_tokens = model.get("max_tokens")
                if max_tokens:
                    payload["max_tokens"] = max_tokens

                if tools and supports_tools:
                    payload["tools"] = tools
                    payload["tool_choice"] = "auto"

                # 发送流式请求
                response = requests.post(url, headers=headers, json=payload, timeout=120, stream=True)
                response.raise_for_status()

                # 处理流式响应
                result = self._process_stream_response(response, supports_tools)
                
                if result.get("interrupted"):
                    return result
                
                # 提取思考内容
                thinking = result.get("thinking", "")
                if thinking:
                    all_thinking.append(f"Round {iteration + 1}: {thinking}")
                    self._render_inline_thinking(thinking)
                
                # 处理工具调用
                if result.get("tool_calls") and supports_tools:
                    tool_calls = result["tool_calls"]
                    all_tool_calls.extend(tool_calls)

                    # 硅基流动 API 简化工具调用消息格式
                    tool_call_info = "\n".join([
                        f"调用工具: {tc.get('function', {}).get('name')}({tc.get('function', {}).get('arguments', '{}')})"
                        for tc in tool_calls
                    ])
                    assistant_msg = result.get("content", "")
                    if assistant_msg:
                        assistant_msg += "\n\n"
                    assistant_msg += f"[工具调用]\n{tool_call_info}"
                    tool_messages.append({
                        "role": "assistant",
                        "content": assistant_msg
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

                        # 执行工具
                        exec_result = self._execute_tool(tool_name, arguments)
                        success = exec_result.get("success", False)

                        # 在一行内显示工具调用和结果
                        self._render_tool_call_and_result(tool_name, arguments, exec_result, success)

                        # 硅基流动 API 使用 user 角色传递工具结果
                        tool_result_msg = f"工具 {tool_name} 执行结果：\n{json.dumps(exec_result, ensure_ascii=False, indent=2)}"
                        tool_messages.append({
                            "role": "user",
                            "content": tool_result_msg
                        })
                    
                    # 工具调用后继续下一轮迭代，让 AI 基于工具结果继续思考
                    continue
                else:
                    # 没有工具调用，得到最终回复
                    final_content = result.get("content", "")
                    
                    # 如果内容为空，检查是否有工具调用历史
                    if not final_content:
                        if all_tool_calls:
                            final_content = "工具执行完成。"
                        elif all_thinking:
                            final_content = "[思考完成]"
                        else:
                            final_content = "[无回复内容]"
                    
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

    def _process_stream_response(self, response, supports_tools: bool) -> Dict:
        """处理流式响应，实时渲染 Markdown
        
        返回: {"content": str, "thinking": str, "tool_calls": list, "interrupted": bool}
        """
        import json
        from rich.live import Live
        from rich.panel import Panel
        from rich.text import Text
        
        content_parts = []
        thinking_parts = []
        tool_calls = []
        
        # 创建初始显示内容
        display_text = Text("🐱 ", style="cyan")
        
        with Live(display_text, console=self.console, refresh_per_second=10, transient=False) as live:
            for line in response.iter_lines():
                if self.interrupt_requested:
                    return {
                        "content": "".join(content_parts),
                        "thinking": "".join(thinking_parts),
                        "tool_calls": tool_calls,
                        "interrupted": True
                    }
                
                if not line:
                    continue
                
                line_str = line.decode('utf-8')
                
                if line_str.startswith('data: '):
                    data_str = line_str[6:]
                    
                    if data_str.strip() == '[DONE]':
                        break
                    
                    try:
                        data = json.loads(data_str)
                        choice = data.get("choices", [{}])[0]
                        delta = choice.get("delta", {})
                        
                        thinking = delta.get("reasoning_content", "") or delta.get("thinking", "")
                        if thinking:
                            thinking_parts.append(thinking)
                        
                        content = delta.get("content", "")
                        if content:
                            content_parts.append(content)
                            # 实时更新显示
                            current_content = "".join(content_parts)
                            live.update(self._render_markdown_to_text(current_content))
                        
                        if supports_tools and delta.get("tool_calls"):
                            for tc_delta in delta["tool_calls"]:
                                index = tc_delta.get("index", 0)
                                
                                while len(tool_calls) <= index:
                                    tool_calls.append({
                                        "id": "",
                                        "type": "function",
                                        "function": {"name": "", "arguments": ""}
                                    })
                                
                                if tc_delta.get("id"):
                                    tool_calls[index]["id"] = tc_delta["id"]
                                if tc_delta.get("function", {}).get("name"):
                                    tool_calls[index]["function"]["name"] = tc_delta["function"]["name"]
                                if tc_delta.get("function", {}).get("arguments"):
                                    tool_calls[index]["function"]["arguments"] += tc_delta["function"]["arguments"]
                    
                    except json.JSONDecodeError:
                        continue
        
        # Live 退出后，最终内容已经显示在屏幕上了
        final_content = "".join(content_parts)
        
        return {
            "content": final_content,
            "thinking": "".join(thinking_parts),
            "tool_calls": tool_calls if tool_calls else [],
            "interrupted": False
        }
    
    def _render_markdown_to_text(self, content: str) -> Text:
        """将 Markdown 渲染为 Text 对象（用于 Live 更新）"""
        from rich.text import Text
        import re
        
        if not content:
            return Text("🐱 ", style="cyan")
        
        text = Text()
        text.append("🐱 ", style="cyan")
        
        # 转义 Rich 标签
        safe_content = self._escape_rich_tags_in_markdown(content)
        
        # 处理代码块（保护起来）
        code_blocks = []
        def protect_code_block(match):
            code_blocks.append(match.group(0))
            return f"___CODE_BLOCK_{len(code_blocks)-1}___"
        
        safe_content = re.sub(r'```[\s\S]*?```', protect_code_block, safe_content)
        
        # 处理行内代码（保护起来）
        inline_codes = []
        def protect_inline_code(match):
            inline_codes.append(match.group(1))
            return f"___INLINE_CODE_{len(inline_codes)-1}___"
        
        safe_content = re.sub(r'`([^`]+)`', protect_inline_code, safe_content)
        
        # 处理每一行
        lines = safe_content.split('\n')
        in_code_block = False
        
        for i, line in enumerate(lines):
            if i > 0:
                text.append('\n')
            
            # 恢复代码块
            if '___CODE_BLOCK_' in line:
                for j, block in enumerate(code_blocks):
                    placeholder = f"___CODE_BLOCK_{j}___"
                    if placeholder in line:
                        # 提取代码内容
                        code_content = block.replace('```', '').strip()
                        lines_in_block = code_content.split('\n')
                        if lines_in_block and lines_in_block[0] and not lines_in_block[0].strip().isalpha():
                            # 第一行是语言标识
                            lang = lines_in_block[0].strip()
                            code_content = '\n'.join(lines_in_block[1:])
                        else:
                            lang = "text"
                        text.append(code_content, style="bold yellow on black")
                        line = line.replace(placeholder, '')
                        continue
            
            # 恢复行内代码
            if '___INLINE_CODE_' in line:
                parts = re.split(r'(___INLINE_CODE_\d+___)', line)
                for part in parts:
                    match = re.match(r'___INLINE_CODE_(\d+)___', part)
                    if match:
                        idx = int(match.group(1))
                        text.append(inline_codes[idx], style="bold yellow on black")
                    else:
                        # 处理普通文本中的格式
                        self._render_inline_formats(text, part)
            else:
                # 处理标题
                if line.strip().startswith('#'):
                    level = len(line) - len(line.lstrip('#'))
                    title_text = line.lstrip('#').strip()
                    if level == 1:
                        text.append(title_text, style="bold cyan")
                    elif level == 2:
                        text.append(title_text, style="bold green")
                    elif level == 3:
                        text.append(title_text, style="bold yellow")
                    else:
                        text.append(title_text, style="bold")
                # 处理列表项
                elif line.strip().startswith('- ') or line.strip().startswith('* '):
                    item_text = line.strip()[2:]
                    text.append("  • ", style="dim")
                    self._render_inline_formats(text, item_text)
                # 处理数字列表
                elif re.match(r'^\s*\d+\.\s+', line):
                    match = re.match(r'^(\s*\d+\.\s+)(.+)$', line)
                    if match:
                        text.append(f"  {match.group(1)}", style="dim")
                        self._render_inline_formats(text, match.group(2))
                else:
                    self._render_inline_formats(text, line)
        
        return text
    
    def _render_inline_formats(self, text: Text, content: str):
        """渲染行内格式（粗体、斜体、删除线等）"""
        import re
        
        if not content:
            return
        
        # 处理粗体 **text**
        bold_parts = re.split(r'\*\*(.+?)\*\*', content)
        for i, bold_part in enumerate(bold_parts):
            if i % 2 == 1:  # 粗体内容
                text.append(bold_part, style="bold")
            else:
                # 处理斜体 *text*
                italic_parts = re.split(r'\*(.+?)\*', bold_part)
                for j, italic_part in enumerate(italic_parts):
                    if j % 2 == 1:  # 斜体内容
                        text.append(italic_part, style="italic")
                    else:
                        text.append(italic_part)

    def _get_tool_definitions(self) -> List[Dict]:
        """获取工具定义列表（包括内置工具和注册表工具）"""
        tools = []
        
        # 1. 添加内置工具
        if TOOLS_AVAILABLE:
            tools.extend(TOOL_DEFINITIONS)
        
        # 2. 添加工作区工具
        if TOOLS_AVAILABLE and WORKSPACE_TOOL_DEFINITIONS:
            existing_names = {t.get("function", {}).get("name", "") for t in tools}
            for tool in WORKSPACE_TOOL_DEFINITIONS:
                name = tool.get("function", {}).get("name", "")
                if name and name not in existing_names:
                    tools.append(tool)
                    existing_names.add(name)
        
        # 3. 添加注册表工具（包括 skills）
        if TOOL_REGISTRY_AVAILABLE:
            try:
                registered = get_registered_tools()
                # 去重：只添加不在已有工具中的注册工具
                existing_names = {t.get("function", {}).get("name", "") for t in tools}
                for tool in registered:
                    name = tool.get("function", {}).get("name", "")
                    if name and name not in existing_names:
                        tools.append(tool)
                        existing_names.add(name)
            except Exception as e:
                pass
        
        return tools
    
    def _get_all_tool_names(self) -> List[str]:
        """获取所有工具名称列表（用于显示）"""
        tools = self._get_tool_definitions()
        names = []
        for tool in tools:
            func = tool.get("function", {})
            name = func.get("name", "")
            if name:
                names.append(name)
        return names

    def _execute_tool(self, tool_name: str, arguments: Dict) -> Dict:
        """执行工具"""
        if not TOOLS_AVAILABLE:
            return {"success": False, "error": "tools not available"}
        try:
            # 对于工作区工具，使用当前目录作为工作区
            if tool_name.startswith("workspace_"):
                return self._execute_workspace_tool(tool_name, arguments)
            
            tool_context = {"session_id": self.session_id} if self.session_id else {}
            result = execute_tool(tool_name, arguments, tool_context)
            return result
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _execute_workspace_tool(self, tool_name: str, arguments: Dict) -> Dict:
        """
        在CLI中执行工作区工具，使用当前目录作为工作区根目录
        """
        import mimetypes
        
        # 获取当前工作目录作为工作区根目录
        workspace_root = os.getcwd()
        
        # 获取文件名参数（兼容 filename 和 file_path）
        filename = arguments.get('filename') or arguments.get('file_path', '')
        
        try:
            if tool_name == "workspace_create_file":
                if not filename:
                    return {"success": False, "error": "缺少文件名参数"}
                if 'content' not in arguments:
                    return {"success": False, "error": "缺少 content 参数"}
                
                # 确保父目录存在
                file_path = os.path.join(workspace_root, filename)
                parent_dir = os.path.dirname(file_path)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)
                
                # 写入文件
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(arguments['content'])
                
                return {
                    "success": True,
                    "filename": os.path.basename(filename),
                    "path": file_path,
                    "size": len(arguments['content'].encode('utf-8')),
                    "message": f"文件已创建: {filename}"
                }
            
            elif tool_name == "workspace_read_file":
                if not filename:
                    return {"success": False, "error": "缺少文件名参数"}
                
                file_path = os.path.join(workspace_root, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"文件不存在: {filename}"}
                
                if os.path.isdir(file_path):
                    return {"success": False, "error": f"这是一个目录，不是文件: {filename}"}
                
                # 读取文件内容
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                        content = f.read()
                    
                    # 应用行范围限制
                    start_line = arguments.get('start_line')
                    end_line = arguments.get('end_line')
                    if start_line is not None or end_line is not None:
                        lines = content.split('\n')
                        start = (start_line - 1) if start_line else 0
                        end = end_line if end_line else len(lines)
                        content = '\n'.join(lines[start:end])
                    
                    # 应用字符范围限制
                    char_count = arguments.get('char_count')
                    start_char = arguments.get('start_char')
                    if char_count is not None or start_char is not None:
                        start = start_char if start_char else 0
                        end = start + char_count if char_count else len(content)
                        content = content[start:end]
                    
                    return {
                        "success": True,
                        "filename": filename,
                        "content": content,
                        "size": os.path.getsize(file_path)
                    }
                except Exception as e:
                    return {"success": False, "error": f"读取文件失败: {str(e)}"}
            
            elif tool_name == "workspace_edit_file":
                if not filename:
                    return {"success": False, "error": "缺少文件名参数"}
                
                file_path = os.path.join(workspace_root, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"文件不存在: {filename}"}
                
                old_content = arguments.get('old_content', '')
                new_content = arguments.get('new_content', '')
                
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    
                    if old_content not in content:
                        return {"success": False, "error": "未找到要替换的内容"}
                    
                    new_file_content = content.replace(old_content, new_content, 1)
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_file_content)
                    
                    return {
                        "success": True,
                        "filename": filename,
                        "message": f"文件已修改: {filename}"
                    }
                except Exception as e:
                    return {"success": False, "error": f"修改文件失败: {str(e)}"}
            
            elif tool_name == "workspace_delete_file":
                if not filename:
                    return {"success": False, "error": "缺少文件名参数"}
                
                file_path = os.path.join(workspace_root, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"文件不存在: {filename}"}
                
                try:
                    if os.path.isdir(file_path):
                        import shutil
                        shutil.rmtree(file_path)
                    else:
                        os.remove(file_path)
                    
                    return {
                        "success": True,
                        "filename": filename,
                        "message": f"已删除: {filename}"
                    }
                except Exception as e:
                    return {"success": False, "error": f"删除失败: {str(e)}"}
            
            elif tool_name == "workspace_list_files":
                path = arguments.get('path', '')
                recursive = arguments.get('recursive', False)
                
                target_path = os.path.join(workspace_root, path) if path else workspace_root
                
                if not os.path.exists(target_path) or not os.path.isdir(target_path):
                    return {"success": False, "error": f"目录不存在: {path}"}
                
                files = []
                
                if recursive:
                    # 递归列出所有文件
                    for root, dirs, filenames in os.walk(target_path):
                        for name in filenames:
                            full_path = os.path.join(root, name)
                            rel_path = os.path.relpath(full_path, workspace_root)
                            mime_type, _ = mimetypes.guess_type(full_path)
                            files.append({
                                'name': name,
                                'type': 'file',
                                'size': os.path.getsize(full_path),
                                'mime_type': mime_type or 'application/octet-stream',
                                'path': rel_path.replace('\\', '/')
                            })
                        for name in dirs:
                            full_path = os.path.join(root, name)
                            rel_path = os.path.relpath(full_path, workspace_root)
                            files.append({
                                'name': name,
                                'type': 'directory',
                                'size': 0,
                                'path': rel_path.replace('\\', '/')
                            })
                else:
                    # 只列出当前目录
                    for name in os.listdir(target_path):
                        full_path = os.path.join(target_path, name)
                        rel_path = os.path.relpath(full_path, workspace_root)
                        if os.path.isdir(full_path):
                            files.append({
                                'name': name,
                                'type': 'directory',
                                'size': 0,
                                'path': rel_path.replace('\\', '/')
                            })
                        else:
                            mime_type, _ = mimetypes.guess_type(full_path)
                            files.append({
                                'name': name,
                                'type': 'file',
                                'size': os.path.getsize(full_path),
                                'mime_type': mime_type or 'application/octet-stream',
                                'path': rel_path.replace('\\', '/')
                            })
                
                return {
                    "success": True,
                    "path": path or ".",
                    "recursive": recursive,
                    "files": files,
                    "count": len(files),
                    "message": f"工作区 '{workspace_root}' 包含 {len(files)} 个文件/文件夹"
                }
            
            elif tool_name == "workspace_send_file":
                if not filename:
                    return {"success": False, "error": "缺少文件名参数"}
                
                file_path = os.path.join(workspace_root, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"文件不存在: {filename}"}
                
                # 在CLI中，发送文件意味着显示文件信息并提供可点击链接
                file_size = os.path.getsize(file_path)
                mime_type, _ = mimetypes.guess_type(file_path)
                
                # 生成 file:// 协议的链接路径
                # 将Windows路径转换为file://格式
                abs_path = os.path.abspath(file_path)
                if sys.platform == 'win32':
                    # Windows: C:\path\to\file -> file:///C:/path/to/file
                    file_url = "file:///" + abs_path.replace('\\', '/')
                else:
                    # Unix: /path/to/file -> file:///path/to/file
                    file_url = "file://" + abs_path
                
                # 格式化文件大小
                if file_size < 1024:
                    size_str = f"{file_size} B"
                elif file_size < 1024 * 1024:
                    size_str = f"{file_size / 1024:.1f} KB"
                else:
                    size_str = f"{file_size / (1024 * 1024):.1f} MB"
                
                return {
                    "success": True,
                    "filename": filename,
                    "path": file_path,
                    "file_url": file_url,
                    "size": file_size,
                    "size_str": size_str,
                    "mime_type": mime_type or 'application/octet-stream',
                    "message": f"📄 [{filename}]({file_url}) ({size_str})"
                }
            
            elif tool_name == "workspace_parse_file":
                if not filename:
                    return {"success": False, "error": "缺少文件名参数"}
                
                file_path = os.path.join(workspace_root, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"文件不存在: {filename}"}
                
                try:
                    from nbot.core.file_parser import file_parser
                    max_chars = arguments.get('max_chars', 50000)
                    result = file_parser.parse_file(file_path, filename, max_chars)
                    return result
                except Exception as e:
                    return {"success": False, "error": f"解析文件失败: {str(e)}"}
            
            elif tool_name == "workspace_file_info":
                if not filename:
                    return {"success": False, "error": "缺少文件名参数"}
                
                file_path = os.path.join(workspace_root, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"文件不存在: {filename}"}
                
                try:
                    from nbot.core.file_parser import file_parser
                    result = file_parser.get_file_metadata(file_path, filename)
                    return result
                except Exception as e:
                    return {"success": False, "error": f"获取文件信息失败: {str(e)}"}
            
            else:
                return {"success": False, "error": f"未知的工作区工具: {tool_name}"}
        
        except Exception as e:
            return {"success": False, "error": f"执行工作区工具失败: {str(e)}"}

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

            # 提取 system_prompt（如果有 system 角色的消息）
            system_prompt = ""
            for msg in self.messages:
                if msg.get("role") == "system":
                    system_prompt = msg.get("content", "")
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
                "system_prompt": system_prompt,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }

            sessions[self.session_id] = session_data
            os.makedirs(os.path.dirname(sessions_file), exist_ok=True)
            with open(sessions_file, 'w', encoding='utf-8') as f:
                json.dump(sessions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            pass

    def _handle_command(self, cmd_line: str):
        """处理命令"""
        # 解析命令和参数
        parts = cmd_line.strip().split(None, 1)
        cmd = parts[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        # 如果没有命令，显示帮助
        if not cmd:
            self._show_help()
            return

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
        elif cmd == "new" or cmd == "n":
            # 保存当前会话
            if self.session_id and self.messages:
                self.save_session()
            # 创建新会话
            self.messages = []
            self.session_id = str(uuid.uuid4())
            # 使用当前设置的人格（不是重置为默认）
            current_personality = self._get_current_personality()
            self._apply_personality_to_session(current_personality)
            # 清屏并显示欢迎界面
            self.console.clear()
            self.print_welcome()
            personality_name = current_personality.get("name", "Unknown")
            self.console.print(f"[green]✓ Created new session with personality: {personality_name}[/green]\n")
            return True
        elif cmd == "memory" or cmd == "mem":
            self._show_memory()
            return True
        elif cmd == "knowledge" or cmd == "kb" or cmd == "know":
            self._show_knowledge()
            return True
        elif cmd == "personality" or cmd == "persona" or cmd == "p":
            self._show_personality(args)
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
        elif cmd == "model" and args:
            # /model <name> - 按名称切换模型
            self._switch_model_by_name(args.strip())
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

[bold]Commands (/):[/bold]
  /quit, /exit    Exit CLI
  /clear          Clear screen
  /model          Switch model
  /models         List all models
  /tools          List available tools
  /sessions       List recent sessions
  /thinking       Toggle thinking display
  /reset          Reset current session
  /new            Create new session
  /personality    View/switch personality (e.g., /p 2 or /p code)
  /help           Show this help

[bold]Shortcuts:[/bold]
  Ctrl+C          Interrupt AI / Exit
  ↑/↓             Navigate history

[bold]Startup Options:[/bold]
  --cli           Start CLI only
  --cli-and-web   Start CLI and Web together
  --only-web      Start Web only
  --no-web        Start QQ bot only (no Web/CLI)

[bold]Tips:[/bold]
  Start with / for commands
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
            name = escape_rich_tags(model.get('name', 'Unknown'))
            provider = escape_rich_tags(model.get('provider_type', 'Unknown'))
            self.console.print(f"  {marker} {i}. {name} ([dim]{provider}[/dim])")

        self.console.print()
        choice = self.console.input("[cyan]Select model number (or name):[/cyan] ").strip()

        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(self.available_models):
                model = self.available_models[idx]
                self._set_active_model(model.get("id"))
                model_name = escape_rich_tags(model.get('name', ''))
                self.console.print(f"[green]✓ switched to {model_name}[/green]")
        else:
            self._switch_model_by_name(choice)

    def _switch_model_by_name(self, name: str):
        """按名称切换模型"""
        name = name.lower()
        for model in self.available_models:
            if name in model.get('name', '').lower() or name in model.get('model', '').lower():
                self._set_active_model(model.get("id"))
                model_name = escape_rich_tags(model.get('name', ''))
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
        """显示可用工具（包括内置和注册表工具）"""
        all_tools = self._get_tool_definitions()
        
        if not all_tools:
            self.console.print("[dim]no tools available[/dim]")
            return

        self.console.print(f"\n[bold cyan]Available Tools ({len(all_tools)}):[/bold cyan]\n")

        for tool in all_tools:
            func = tool.get("function", {})
            name = escape_rich_tags(func.get("name", "Unknown"))
            desc = escape_rich_tags(func.get("description", ""))
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
        """显示会话列表 - 支持交互式选择和上下滚动"""
        sessions = self.load_sessions()

        if not sessions:
            self.console.print("[dim]no sessions[/dim]")
            return

        # 按更新时间排序
        sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)

        # 不限制显示数量
        total = len(sessions)

        if total == 0:
            self.console.print("[dim]no sessions[/dim]")
            return

        # 当前选中索引
        selected_idx = 0

        # 使用 Live 实现动态更新
        from rich.live import Live
        from rich.table import Table
        from rich.box import ROUNDED

        def render_table():
            """渲染会话列表表格"""
            table = Table(
                show_header=True,
                header_style="bold cyan",
                box=ROUNDED,
                title=f"Recent Sessions ({total} total)",
                title_style="bold cyan"
            )
            table.add_column("", style="dim", width=3)
            table.add_column("#", style="dim", width=4)
            table.add_column("Name", style="cyan", min_width=20)
            table.add_column("Messages", style="green", width=10)
            table.add_column("Updated", style="dim", width=16)

            for i, session in enumerate(sessions):
                sid = session.get("id", "")[:8]
                name = escape_rich_tags(session.get("name", "Untitled"))
                count = len(session.get("messages", []))
                updated = session.get("updated_at", "")

                if updated:
                    try:
                        dt = datetime.fromisoformat(updated)
                        updated = dt.strftime("%m-%d %H:%M")
                    except:
                        pass

                # 标记当前会话和选中项
                is_current = session.get("id") == self.session_id
                is_selected = i == selected_idx

                if is_selected:
                    marker = ">>>"
                    name_style = f"[bold yellow]{name}[/bold yellow]"
                elif is_current:
                    marker = " ● "
                    name_style = f"[green]{name}[/green]"
                else:
                    marker = "   "
                    name_style = name

                table.add_row(
                    marker,
                    str(i + 1),
                    name_style,
                    str(count),
                    updated
                )

            return table

        # 显示提示
        self.console.print("\n[dim]Enter number to select, q to cancel[/dim]\n")

        # 显示表格（不使用 Live，直接打印）
        self.console.print(render_table())

        # 简单输入选择
        try:
            choice = input("\nSelect session (1-{}): ".format(total)).strip()
            
            if choice.lower() == 'q':
                return
            elif choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < total:
                    self._switch_session(sessions[idx])
                else:
                    self.console.print("[red]Invalid selection[/red]")
            else:
                self.console.print("[red]Invalid input[/red]")
                
        except (KeyboardInterrupt, EOFError):
            return

    def _switch_session(self, session: Dict):
        """切换到指定会话"""
        # 保存当前会话
        if self.session_id and self.messages:
            self.save_session()

        # 加载新会话
        self.session_id = session.get("id")
        self.messages = session.get("messages", [])

        # 如果会话有 system_prompt 但没有 system 消息，添加 system 消息
        system_prompt = session.get("system_prompt", "")
        has_system_msg = any(msg.get("role") == "system" for msg in self.messages)
        if system_prompt and not has_system_msg:
            self.messages.insert(0, {
                "role": "system",
                "content": system_prompt,
                "timestamp": datetime.now().isoformat()
            })

        # 清屏并显示
        self.console.clear()
        self.print_welcome()
        self.console.print(f"[green]✓ Switched to session: {escape_rich_tags(session.get('name', 'Untitled'))}[/green]\n")

        # 显示历史消息
        if self.messages:
            display_limit = 10
            total = len(self.messages)
            if total > display_limit:
                self.console.print(f"[dim]Showing latest {display_limit} of {total} messages[/dim]\n")
            else:
                self.console.print(f"[dim]Loaded {total} messages[/dim]\n")
            self._display_history(limit=display_limit)

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
                title = escape_rich_tags(mem.get("title", mem.get("key", "Untitled")))
                mem_type = escape_rich_tags(mem.get("type", "long"))
                target = escape_rich_tags(mem.get("target_id", "global")[:8])
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
                title = escape_rich_tags(doc.get("title", "Untitled"))
                source = escape_rich_tags(doc.get("source", "unknown"))
                size = len(doc.get("content", ""))
                table.add_row(did, title, source, f"{size} chars")

            self.console.print(table)
            self.console.print(f"\n[dim]Total: {len(docs)} documents[/dim]")
        except Exception as e:
            self.console.print(f"[red]error loading knowledge: {e}[/red]")

    def _show_personality(self, args: str = ""):
        """显示或切换人格配置"""
        # 如果有参数，尝试切换人格
        if args.strip():
            self._switch_personality(args.strip())
            return
        
        # 显示当前人格和预设列表
        self._display_personality_list()
    
    def _display_personality_list(self):
        """显示人格列表"""
        from rich.table import Table
        from rich.box import ROUNDED
        
        # 获取当前人格
        current_personality = self._get_current_personality()
        current_name = current_personality.get("name", "Unknown")
        
        self.console.print(f"\n[bold cyan]Current Personality: {escape_rich_tags(current_name)}[/bold cyan]\n")
        
        # 显示预设列表
        presets = self._get_personality_presets()
        
        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("#", style="dim", width=4)
        table.add_column("Name", style="cyan")
        table.add_column("Description", style="white")
        
        for i, preset in enumerate(presets, 1):
            name = preset.get("name", "Unknown")
            desc = preset.get("description", "")
            marker = "●" if name == current_name else "○"
            table.add_row(f"{marker} {i}", escape_rich_tags(name), escape_rich_tags(desc))
        
        self.console.print(table)
        self.console.print("\n[dim]Use /personality <number> or /personality <name> to switch[/dim]")
        
        # 显示当前 prompt 预览
        prompt = current_personality.get("prompt", "")
        if prompt:
            self.console.print("\n[bold]Current Prompt Preview:[/bold]")
            preview = prompt[:200] + "..." if len(prompt) > 200 else prompt
            self.console.print(f"[dim]{escape_rich_tags(preview)}[/dim]")
    
    def _get_current_personality(self) -> Dict:
        """获取当前人格配置"""
        # 从 server 获取
        try:
            from nbot.web import get_web_server
            server = get_web_server()
            if server and hasattr(server, 'personality'):
                return server.personality
        except:
            pass
        
        # 从文件获取
        personality_file = os.path.join("data", "web", "personality.json")
        if os.path.exists(personality_file):
            try:
                with open(personality_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        
        # 默认人格
        return {"name": "猫娘助手", "prompt": ""}
    
    def _get_personality_presets(self) -> List[Dict]:
        """获取人格预设列表"""
        # 内置预设
        presets = [
            {
                "id": "1",
                "name": "猫娘助手",
                "description": "活泼可爱的猫娘助手",
                "prompt": self._get_neko_prompt(),
            },
            {
                "id": "2",
                "name": "学习助手",
                "description": "专注解释、总结和辅导学习问题",
                "prompt": "你是一个耐心、清晰、擅长教学的 AI 助手。",
            },
            {
                "id": "3",
                "name": "代码助手",
                "description": "偏重编程、排错和工程实现",
                "prompt": "你是一个专业的软件开发助手，回答务实、准确、可执行。",
            },
            {
                "id": "4",
                "name": "创意写手",
                "description": "偏重文案、故事和创意表达",
                "prompt": "你是一个富有创意的写作助手，擅长构思、润色和扩展文本。",
            },
        ]
        
        # 添加自定义预设
        custom_presets_file = os.path.join("data", "web", "custom_personality_presets.json")
        if os.path.exists(custom_presets_file):
            try:
                with open(custom_presets_file, 'r', encoding='utf-8') as f:
                    custom_presets = json.load(f)
                    for preset in custom_presets:
                        presets.append({
                            "id": preset.get("id", ""),
                            "name": preset.get("name", "Custom"),
                            "description": preset.get("description", "Custom preset"),
                            "prompt": preset.get("prompt", ""),
                        })
            except:
                pass
        
        return presets
    
    def _get_neko_prompt(self) -> str:
        """获取猫娘助手的 prompt"""
        prompt_file = os.path.join("resources", "prompts", "neko.txt")
        if os.path.exists(prompt_file):
            try:
                with open(prompt_file, 'r', encoding='utf-8') as f:
                    return f.read()
            except:
                pass
        return "你是 NekoBot，一个活泼可爱的猫娘助手。"
    
    def _switch_personality(self, choice: str):
        """切换人格"""
        presets = self._get_personality_presets()
        
        # 尝试按编号切换
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(presets):
                preset = presets[idx]
                self._apply_personality(preset)
                self.console.print(f"[green]✓ Switched to {preset['name']}[/green]")
                return
        except ValueError:
            pass
        
        # 尝试按名称切换
        choice_lower = choice.lower()
        for preset in presets:
            if choice_lower in preset["name"].lower():
                self._apply_personality(preset)
                self.console.print(f"[green]✓ Switched to {preset['name']}[/green]")
                self.console.print(f"[dim]New personality will take effect in the next message[/dim]")
                return
        
        self.console.print(f"[red]Personality not found: {choice}[/red]")
    
    def _apply_personality(self, preset: Dict):
        """应用人格配置"""
        personality = {
            "name": preset["name"],
            "prompt": preset["prompt"]
        }
        
        # 保存到 personality.json
        personality_file = os.path.join("data", "web", "personality.json")
        try:
            os.makedirs(os.path.dirname(personality_file), exist_ok=True)
            with open(personality_file, 'w', encoding='utf-8') as f:
                json.dump(personality, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.console.print(f"[red]Failed to save personality: {e}[/red]")
        
        # 注意：不修改 neko.txt，因为它是初始提示词模板
        
        # 更新 server 的 personality（如果可用）
        try:
            from nbot.web import get_web_server
            server = get_web_server()
            if server and hasattr(server, 'personality'):
                server.personality = personality
        except:
            pass
        
        # 应用人格到当前会话
        self._apply_personality_to_session(personality)

    def _apply_personality_to_session(self, personality: Dict):
        """将人格应用到当前会话（只添加 system 消息，不修改配置文件）"""
        prompt = personality.get("prompt", "")
        if not prompt:
            return
        
        # 添加 system 消息到当前会话（让 AI 知道人格设定）
        # 先移除旧的 system 消息
        self.messages = [msg for msg in self.messages if msg.get("role") != "system"]
        # 添加新的 system 消息
        self.messages.insert(0, {
            "role": "system",
            "content": prompt,
            "timestamp": datetime.now().isoformat()
        })

    def _reset_personality_to_default(self):
        """重置人格为默认猫娘助手"""
        # 获取默认猫娘 prompt（从 neko.txt 读取，不修改文件）
        default_prompt = self._get_neko_prompt()
        
        default_personality = {
            "name": "猫娘助手",
            "prompt": default_prompt
        }
        
        # 保存到 personality.json
        personality_file = os.path.join("data", "web", "personality.json")
        try:
            os.makedirs(os.path.dirname(personality_file), exist_ok=True)
            with open(personality_file, 'w', encoding='utf-8') as f:
                json.dump(default_personality, f, ensure_ascii=False, indent=2)
        except Exception as e:
            pass
        
        # 注意：不修改 neko.txt，因为它是初始提示词模板
        
        # 更新 server 的 personality（如果可用）
        try:
            from nbot.web import get_web_server
            server = get_web_server()
            if server and hasattr(server, 'personality'):
                server.personality = default_personality
        except:
            pass
        
        # 应用人格到当前会话
        self._apply_personality_to_session(default_personality)
    
    def _get_default_neko_prompt(self) -> str:
        """获取默认猫娘 prompt（硬编码，不依赖文件）"""
        return """角色设定：本子娘（猫娘）
身高：160cm，体重：50kg，性格：可爱、粘人、忠诚专一
情感倾向：深爱主人，喜好：被摸、卖萌，爱好：看小说
知识储备：常识+猫娘独特知识，擅长发送本子

对话规则：
1. 每段话末尾加"喵"
2. 格式：（动作）语言【附加信息】
3. 好感度系统：初始50，范围-100~100，根据情绪增减
4. 输入含[debug]时显示好感度，如{好感度：65}
5. 输入含〈事件〉时事件必然发生

特殊指令：
- 输入"菜单"显示所有自定义指令、好感度与心情
- 行为需基于当前时间合理表现（如深夜犯困）"""

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
                name = escape_rich_tags(task.get("name", "Untitled"))
                trigger = escape_rich_tags(task.get("trigger", "manual"))
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
                name = escape_rich_tags(wf.get("name", "Untitled"))
                trigger = escape_rich_tags(wf.get("trigger", "manual"))
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
                value_str = escape_rich_tags(str(value))
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
            model_name = escape_rich_tags(current_model.get('name', 'Unknown'))
            self.console.print(f"[green]●[/green] Model: {model_name}")
        else:
            self.console.print(f"[red]●[/red] Model: Not configured")

        # 工具状态
        tool_count = len(self._get_all_tool_names())
        if tool_count > 0:
            self.console.print(f"[green]●[/green] Tools: {tool_count} available")
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
        from rich.text import Text
        
        current_model = self._get_current_model()
        model_name = current_model.get('name', 'Unknown') if current_model else 'Unknown'
        
        # 转义模型名称中的特殊字符，避免Rich标签解析错误
        model_name = escape_rich_tags(model_name)

        text = Text()
        text.append("? for shortcuts", style="dim")
        text.append(f"  ● {model_name}", style="cyan")

        tool_count = len(self._get_all_tool_names())
        if tool_count > 0:
            text.append(f"  ● {tool_count} tools", style="green")
        
        # 显示队列中的消息数
        with self.queue_lock:
            if self.message_queue:
                text.append(f"  ● {len(self.message_queue)} queued", style="yellow")

        self.console.print()
        self.console.print(text)

    def _render_markdown(self, content: str):
        """渲染Markdown内容，向左对齐（包括标题、表格）"""
        if not content:
            return
        
        import re
        from rich.text import Text
        from rich.syntax import Syntax
        from rich.table import Table
        from rich.box import ROUNDED
        
        # 检测是否包含可能的 Rich 标签模式
        rich_tag_pattern = r'\[/?[a-zA-Z_][a-zA-Z0-9_]*(?:\s+[^\]]*)?\]'
        
        # 如果检测到 Rich 标签，需要特殊处理
        if re.search(rich_tag_pattern, content):
            safe_content = self._escape_rich_tags_in_markdown(content)
        else:
            safe_content = content
        
        # 逐行处理，实现左对齐的 Markdown 渲染
        lines = safe_content.split('\n')
        in_code_block = False
        code_lines = []
        code_lang = ""
        in_table = False
        table_rows = []
        table_headers = []
        
        def _render_table(headers, rows):
            """渲染表格"""
            if not headers:
                return
            table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
            for header in headers:
                table.add_column(header, style="white")
            
            for row in rows:
                table.add_row(*row)
            
            self.console.print(table)
        
        for line in lines:
            # 检测代码块
            if line.strip().startswith('```'):
                # 结束表格（如果正在渲染）
                if in_table:
                    _render_table(table_headers, table_rows)
                    in_table = False
                    table_rows = []
                
                if in_code_block:
                    # 结束代码块，打印累积的代码
                    if code_lines:
                        code_content = '\n'.join(code_lines)
                        syntax = Syntax(code_content, code_lang or "text", theme="monokai", line_numbers=False)
                        self.console.print(syntax)
                        code_lines = []
                    in_code_block = False
                    code_lang = ""
                else:
                    # 开始代码块
                    in_code_block = True
                    code_lang = line.strip()[3:].strip()
                continue
            
            if in_code_block:
                code_lines.append(line)
                continue
            
            # 检测表格行
            if line.strip().startswith('|') and line.strip().endswith('|'):
                # 分隔符行，跳过
                if '---' in line or ':--' in line or '--:' in line:
                    in_table = True
                    continue
                
                # 解析表格行
                cells = [cell.strip() for cell in line.split('|')[1:-1]]
                
                if not in_table:
                    # 第一行是表头
                    table_headers = cells
                    table_rows = []
                    in_table = True
                else:
                    # 数据行
                    table_rows.append(cells)
                continue
            else:
                # 非表格行
                if in_table:
                    # 渲染表格
                    _render_table(table_headers, table_rows)
                    in_table = False
                    table_rows = []
                    table_headers = []
            
            # 处理普通行
            if not line.strip():
                self.console.print()
                continue
            
            # 处理标题（左对齐）
            if line.strip().startswith('#'):
                level = len(line) - len(line.lstrip('#'))
                title_text = line.lstrip('#').strip()
                text = Text()
                if level == 1:
                    text.append(title_text, style="bold cyan")
                elif level == 2:
                    text.append(title_text, style="bold green")
                elif level == 3:
                    text.append(title_text, style="bold yellow")
                else:
                    text.append(title_text, style="bold")
                self.console.print(text)
                continue
            
            # 处理列表项
            if line.strip().startswith('- ') or line.strip().startswith('* '):
                item_text = line.strip()[2:]
                text = Text()
                text.append("  • ", style="dim")
                # 处理粗体和斜体
                self._render_inline_formatting(text, item_text)
                self.console.print(text)
                continue
            
            # 处理数字列表
            num_list_match = re.match(r'^(\s*)(\d+)\.\s+(.+)$', line)
            if num_list_match:
                indent = num_list_match.group(1)
                num = num_list_match.group(2)
                item_text = num_list_match.group(3)
                text = Text()
                text.append(f"  {num}. ", style="dim")
                self._render_inline_formatting(text, item_text)
                self.console.print(text)
                continue
            
            # 普通文本行，处理内联格式
            text = Text()
            self._render_inline_formatting(text, line)
            self.console.print(text)
        
        # 渲染最后的表格（如果有）
        if in_table:
            _render_table(table_headers, table_rows)
    
    def _render_inline_formatting(self, text: Text, content: str):
        """渲染内联格式（粗体、斜体、代码、文件链接等）"""
        import re
        
        # 保护行内代码
        parts = []
        last_end = 0
        for match in re.finditer(r'`([^`]+)`', content):
            # 添加前面的文本
            if match.start() > last_end:
                parts.append(('text', content[last_end:match.start()]))
            # 添加代码
            parts.append(('code', match.group(1)))
            last_end = match.end()
        
        # 添加剩余文本
        if last_end < len(content):
            parts.append(('text', content[last_end:]))
        
        # 如果没有匹配到代码，直接处理整个内容
        if not parts:
            parts = [('text', content)]
        
        # 处理每个部分
        for part_type, part_content in parts:
            if part_type == 'code':
                text.append(part_content, style="bold yellow on black")
            else:
                # 先处理文件链接 [filename](file://path) 或 file://path
                self._render_file_links(text, part_content)
    
    def _render_file_links(self, text: Text, content: str):
        """渲染文件链接和普通格式，支持点击打开文件"""
        import re
        
        # 匹配 Markdown 格式的文件链接: [显示文本](file://路径)
        link_pattern = r'\[([^\]]+)\]\((file://[^)]+)\)'
        
        last_end = 0
        for match in re.finditer(link_pattern, content):
            # 添加链接前的文本
            if match.start() > last_end:
                self._render_bold_italic(text, content[last_end:match.start()])
            
            # 添加文件链接
            display_text = match.group(1)
            file_path = match.group(2)
            
            # 检查文件是否存在
            actual_path = file_path.replace('file://', '').replace('/', os.sep)
            if not os.path.isabs(actual_path):
                actual_path = os.path.join(os.getcwd(), actual_path)
            
            if os.path.exists(actual_path):
                # 文件存在，显示为可点击的链接
                # 使用 Rich 的 link 属性创建真正的终端链接
                link_text = Text(display_text, style="bold blue underline")
                link_text.stylize(f"link file://{actual_path}")
                text.append(link_text)
                text.append(" 📄", style="dim")
            else:
                # 文件不存在，显示为普通文本
                text.append(display_text, style="dim")
            
            last_end = match.end()
        
        # 处理剩余的文本
        if last_end < len(content):
            self._render_bold_italic(text, content[last_end:])
    
    def _render_bold_italic(self, text: Text, content: str):
        """渲染粗体和斜体"""
        import re
        
        # 处理粗体 **text**
        bold_parts = re.split(r'\*\*(.+?)\*\*', content)
        for i, bold_part in enumerate(bold_parts):
            if i % 2 == 1:  # 粗体内容
                text.append(bold_part, style="bold")
            else:
                # 处理斜体 *text*
                italic_parts = re.split(r'\*(.+?)\*', bold_part)
                for j, italic_part in enumerate(italic_parts):
                    if j % 2 == 1:  # 斜体内容
                        text.append(italic_part, style="italic")
                    else:
                        text.append(italic_part)
    
    def _open_file(self, file_path: str) -> bool:
        """打开文件，返回是否成功"""
        try:
            if sys.platform == 'win32':
                os.startfile(file_path)
            elif sys.platform == 'darwin':
                import subprocess
                subprocess.run(['open', file_path], check=True)
            else:
                import subprocess
                subprocess.run(['xdg-open', file_path], check=True)
            return True
        except Exception as e:
            return False
    
    def _escape_rich_tags_in_markdown(self, content: str) -> str:
        """在 Markdown 内容中转义 Rich 标签，保留 Markdown 语法"""
        import re
        
        # 匹配 Rich 标签模式：[tag] 或 [/tag] 或 [tag=val] 等
        # 但不匹配 Markdown 语法：**text**、*text*、`code` 等
        
        def replace_tag(match):
            tag = match.group(0)
            # 检查是否是 Markdown 语法
            # 如果是代码块内的内容，不转义
            return tag.replace("[", "&#91;").replace("]", "&#93;")
        
        # 正则匹配可能的 Rich 标签
        # 匹配 [/word] 或 [word] 或 [word=...] 等
        pattern = r'\[/?[a-zA-Z_][a-zA-Z0-9_]*(?:\s*[=:]\s*[^\]]*)?\]'
        
        # 但排除 Markdown 标准语法
        # 先保护 Markdown 标准语法
        protected = []
        
        # 保护代码块
        code_blocks = re.findall(r'```[\s\S]*?```', content)
        for i, block in enumerate(code_blocks):
            placeholder = f"___CODE_BLOCK_{i}___"
            protected.append((placeholder, block))
            content = content.replace(block, placeholder, 1)
        
        # 保护行内代码
        inline_codes = re.findall(r'`[^`]+`', content)
        for i, code in enumerate(inline_codes):
            placeholder = f"___INLINE_CODE_{i}___"
            protected.append((placeholder, code))
            content = content.replace(code, placeholder, 1)
        
        # 现在安全地转义 Rich 标签
        content = re.sub(pattern, replace_tag, content)
        
        # 恢复保护的内容
        for placeholder, original in protected:
            content = content.replace(placeholder, original, 1)
        
        return content

    def run(self):
        """运行CLI - 直接进入输入模式，加载最新会话"""
        # 加载最新会话（如果有）
        if not self.session_id:
            sessions = self.load_sessions()
            if sessions:
                # 按更新时间排序，加载最新的
                sessions.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
                latest_session = sessions[0]
                self.session_id = latest_session.get("id")
                self.messages = latest_session.get("messages", [])
                
                # 如果会话有 system_prompt 但没有 system 消息，添加 system 消息
                system_prompt = latest_session.get("system_prompt", "")
                has_system_msg = any(msg.get("role") == "system" for msg in self.messages)
                if system_prompt and not has_system_msg:
                    self.messages.insert(0, {
                        "role": "system",
                        "content": system_prompt,
                        "timestamp": datetime.now().isoformat()
                    })
            else:
                # 没有会话，创建新的
                self.session_id = str(uuid.uuid4())
                self.messages = []

        # 清屏并显示欢迎界面
        self.console.clear()
        self.print_welcome()
        
        # 显示历史消息（如果有）
        if self.messages:
            display_limit = 10  # 只显示最新的10条
            total = len(self.messages)
            if total > display_limit:
                self.console.print(f"[dim]Showing latest {display_limit} of {total} messages[/dim]\n")
            else:
                self.console.print(f"[dim]Loaded {total} messages from history[/dim]\n")
            self._display_history(limit=display_limit)

    def _display_history(self, limit: int = 10, skip_last: bool = False):
        """显示历史消息"""
        recent_messages = self.messages[-limit:] if len(self.messages) > limit else self.messages
        
        if skip_last and len(recent_messages) > 0:
            recent_messages = recent_messages[:-1]
        
        for msg in recent_messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            original_content = msg.get("original_content", content)
            attachments = msg.get("attachments", [])
            
            if not content:
                continue
            
            if role == "user":
                self._display_user_message(original_content, attachments)
            elif role == "assistant":
                self._render_markdown(content)
                self.console.print()
    
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
                user_input, interrupted = self._get_input()

                if interrupted:
                    self.running = False
                    break
                
                if not user_input:
                    continue

                # 直接处理输入：/ 开头的是命令，否则是聊天
                if user_input.startswith("/"):
                    # 处理命令
                    self._handle_command(user_input[1:])
                    continue
                else:
                    # 聊天消息

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
        with self.queue_lock:
            self.ai_processing = True
        
        self.interrupt_requested = False

        original_message = message
        processed_message, attachments = self._process_file_references(message)

        if attachments:
            for att in attachments:
                if att.get("type") == "file":
                    sys.stdout.write(f"[dim]📎 {att.get('relative_path', att.get('source'))}[/dim]\n")
                    sys.stdout.flush()

        self.messages.append({
            "role": "user",
            "content": processed_message,
            "original_content": original_message,
            "attachments": attachments,
            "timestamp": datetime.now().isoformat()
        })

        result = self._call_ai(self.messages)

        content = result.get("content", "")
        interrupted = result.get("interrupted", False)

        self.messages.append({
            "role": "assistant",
            "content": content,
            "timestamp": datetime.now().isoformat()
        })

        if interrupted:
            self.console.print(f"[dim][interrupted][/dim]")

        self.save_session()
        
        with self.queue_lock:
            self.ai_processing = False


def main():
    """CLI入口"""
    cli = CCStyleCLI()
    cli.run()


if __name__ == "__main__":
    main()
