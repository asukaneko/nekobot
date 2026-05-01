"""
CLI 界面组件 - 使用 Rich 库构建美观的UI组件
"""

from datetime import datetime
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.box import ROUNDED, HEAVY
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.align import Align


@dataclass
class Message:
    """消息数据类"""
    role: str  # 'user', 'assistant', 'system', 'tool'
    content: str
    timestamp: datetime = None
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()
        if self.metadata is None:
            self.metadata = {}


class Header:
    """顶部标题栏组件"""
    
    def __init__(self, title: str = "NekoBot CLI", subtitle: str = ""):
        self.title = title
        self.subtitle = subtitle
        self.console = Console()
        
    def render(self, current_screen: str = "main") -> Panel:
        """渲染标题栏"""
        # 构建标题文本
        title_text = Text()
        title_text.append("🐱 ", style="bold yellow")
        title_text.append(self.title, style="bold cyan")
        
        if self.subtitle:
            title_text.append(f" | {self.subtitle}", style="dim")
        
        # 当前屏幕指示器
        screen_indicators = {
            "main": "[cyan]●[/cyan] 主页",
            "chat": "[green]●[/green] 聊天",
            "tools": "[blue]●[/blue] 工具",
            "config": "[magenta]●[/magenta] 配置",
            "sessions": "[yellow]●[/yellow] 会话",
        }
        
        right_text = Text()
        for screen, label in screen_indicators.items():
            if screen == current_screen:
                right_text.append(f"{label}  ", style="bold")
            else:
                right_text.append(f"{label.replace('[cyan]', '').replace('[green]', '').replace('[blue]', '').replace('[magenta]', '').replace('[yellow]', '').replace('[/cyan]', '').replace('[/green]', '').replace('[/blue]', '').replace('[/magenta]', '').replace('[/yellow]', '')}  ", style="dim")
        
        # 时间显示
        time_str = datetime.now().strftime("%H:%M:%S")
        right_text.append(f"| {time_str}", style="dim")
        
        content = Table.grid(expand=True)
        content.add_column(justify="left", ratio=1)
        content.add_column(justify="right")
        content.add_row(title_text, right_text)
        
        return Panel(
            content,
            box=HEAVY,
            style="cyan",
            padding=(0, 1),
            height=3
        )


class Footer:
    """底部状态栏组件"""
    
    def __init__(self):
        self.status = "就绪"
        self.shortcuts = [
            ("Ctrl+C", "退出"),
            ("Tab", "切换"),
            ("Enter", "确认"),
            ("/", "命令"),
            ("?", "帮助"),
        ]
        
    def render(self) -> Panel:
        """渲染底部状态栏"""
        # 快捷键显示
        shortcut_text = Text()
        for key, desc in self.shortcuts:
            shortcut_text.append(f"[{key}]", style="bold cyan")
            shortcut_text.append(f"{desc}  ", style="dim")
        
        # 状态显示
        status_text = Text()
        status_text.append("状态: ", style="dim")
        status_text.append(self.status, style="green" if "就绪" in self.status else "yellow")
        
        content = Table.grid(expand=True)
        content.add_column(justify="left", ratio=1)
        content.add_column(justify="right")
        content.add_row(shortcut_text, status_text)
        
        return Panel(
            content,
            box=HEAVY,
            style="dim",
            padding=(0, 1),
            height=3
        )
    
    def set_status(self, status: str):
        """设置状态文本"""
        self.status = status


class Sidebar:
    """侧边栏组件"""
    
    def __init__(self, width: int = 30):
        self.width = width
        self.items: List[Dict[str, Any]] = []
        self.selected_index = 0
        
    def set_items(self, items: List[Dict[str, Any]]):
        """设置侧边栏项目"""
        self.items = items
        
    def select_next(self):
        """选择下一个"""
        if self.items:
            self.selected_index = (self.selected_index + 1) % len(self.items)
            
    def select_previous(self):
        """选择上一个"""
        if self.items:
            self.selected_index = (self.selected_index - 1) % len(self.items)
            
    def get_selected(self) -> Optional[Dict[str, Any]]:
        """获取当前选中的项目"""
        if self.items and 0 <= self.selected_index < len(self.items):
            return self.items[self.selected_index]
        return None
        
    def render(self, title: str = "菜单") -> Panel:
        """渲染侧边栏"""
        content = Text()
        
        for i, item in enumerate(self.items):
            icon = item.get("icon", "•")
            label = item.get("label", "Unknown")
            desc = item.get("description", "")
            
            if i == self.selected_index:
                # 选中项高亮
                content.append(f" {icon} ", style="bold cyan")
                content.append(f"{label}", style="bold white on cyan")
                if desc:
                    content.append(f" - {desc}", style="dim")
                content.append("\n")
            else:
                content.append(f" {icon} ", style="dim")
                content.append(f"{label}", style="white")
                if desc:
                    content.append(f" - {desc}", style="dim")
                content.append("\n")
        
        return Panel(
            content,
            title=f"[bold]{title}[/bold]",
            box=ROUNDED,
            style="cyan",
            width=self.width,
            padding=(1, 1)
        )


class MessagePanel:
    """消息面板组件 - 显示聊天消息"""
    
    def __init__(self):
        self.messages: List[Message] = []
        self.max_messages = 100
        
    def add_message(self, message: Message):
        """添加消息"""
        self.messages.append(message)
        # 限制消息数量
        if len(self.messages) > self.max_messages:
            self.messages = self.messages[-self.max_messages:]
            
    def clear(self):
        """清空消息"""
        self.messages = []
        
    def render(self) -> Panel:
        """渲染消息面板"""
        if not self.messages:
            return Panel(
                Align.center(Text("暂无消息，开始聊天吧~", style="dim")),
                title="[bold]消息[/bold]",
                box=ROUNDED,
                style="cyan"
            )
        
        message_texts = []
        
        for msg in self.messages[-20:]:  # 只显示最近20条
            time_str = msg.timestamp.strftime("%H:%M")
            
            if msg.role == "user":
                header = Text(f"[{time_str}] ", style="dim")
                header.append("你", style="bold green")
                content = Text(msg.content, style="white")
                
            elif msg.role == "assistant":
                header = Text(f"[{time_str}] ", style="dim")
                header.append("🐱 NekoBot", style="bold cyan")
                content = Text(msg.content, style="white")
                
            elif msg.role == "system":
                header = Text(f"[{time_str}] ", style="dim")
                header.append("系统", style="bold yellow")
                content = Text(msg.content, style="dim")
                
            elif msg.role == "tool":
                header = Text(f"[{time_str}] ", style="dim")
                header.append("🔧 工具", style="bold blue")
                content = Text(msg.content, style="dim")
                
            else:
                header = Text(f"[{time_str}] ", style="dim")
                header.append(msg.role, style="bold")
                content = Text(msg.content, style="white")
            
            message_texts.append(header)
            message_texts.append(content)
            message_texts.append(Text())  # 空行
        
        content_group = Group(*message_texts)
        
        return Panel(
            content_group,
            title="[bold]消息[/bold]",
            box=ROUNDED,
            style="cyan",
            padding=(1, 2)
        )


class InputBox:
    """输入框组件"""
    
    def __init__(self, placeholder: str = "输入消息..."):
        self.placeholder = placeholder
        self.content = ""
        self.cursor_position = 0
        
    def render(self) -> Panel:
        """渲染输入框"""
        if self.content:
            display_text = Text(f"> {self.content}", style="white")
            # 添加光标
            display_text.append("█", style="bold cyan")
        else:
            display_text = Text(f"> {self.placeholder}", style="dim")
            display_text.append("█", style="bold cyan")
        
        return Panel(
            display_text,
            box=ROUNDED,
            style="green",
            padding=(0, 1),
            height=3
        )
    
    def set_content(self, content: str):
        """设置内容"""
        self.content = content
        self.cursor_position = len(content)
        
    def append_char(self, char: str):
        """追加字符"""
        self.content = self.content[:self.cursor_position] + char + self.content[self.cursor_position:]
        self.cursor_position += 1
        
    def backspace(self):
        """退格"""
        if self.cursor_position > 0:
            self.content = self.content[:self.cursor_position-1] + self.content[self.cursor_position:]
            self.cursor_position -= 1
            
    def delete(self):
        """删除"""
        if self.cursor_position < len(self.content):
            self.content = self.content[:self.cursor_position] + self.content[self.cursor_position+1:]
            
    def move_cursor_left(self):
        """光标左移"""
        if self.cursor_position > 0:
            self.cursor_position -= 1
            
    def move_cursor_right(self):
        """光标右移"""
        if self.cursor_position < len(self.content):
            self.cursor_position += 1
            
    def clear(self):
        """清空"""
        self.content = ""
        self.cursor_position = 0
        
    def get_content(self) -> str:
        """获取内容"""
        return self.content


class ToolPanel:
    """工具面板组件"""
    
    def __init__(self):
        self.tools: List[Dict[str, Any]] = []
        self.selected_index = 0
        
    def set_tools(self, tools: List[Dict[str, Any]]):
        """设置工具列表"""
        self.tools = tools
        
    def render(self) -> Panel:
        """渲染工具面板"""
        if not self.tools:
            return Panel(
                Align.center(Text("暂无可用工具", style="dim")),
                title="[bold]工具[/bold]",
                box=ROUNDED,
                style="cyan"
            )
        
        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("名称", style="cyan", width=20)
        table.add_column("描述", style="white")
        table.add_column("状态", style="green", width=10)
        
        for i, tool in enumerate(self.tools):
            name = tool.get("name", "Unknown")
            description = tool.get("description", "")
            enabled = tool.get("enabled", True)
            
            status = "✓ 启用" if enabled else "✗ 禁用"
            status_style = "green" if enabled else "red"
            
            if i == self.selected_index:
                name = f"> {name}"
                
            table.add_row(
                Text(name, style="bold" if i == self.selected_index else ""),
                description,
                Text(status, style=status_style)
            )
        
        return Panel(
            table,
            title="[bold]可用工具[/bold]",
            box=ROUNDED,
            style="cyan",
            padding=(1, 1)
        )


class SessionPanel:
    """会话列表面板"""
    
    def __init__(self):
        self.sessions: List[Dict[str, Any]] = []
        self.selected_index = 0
        
    def set_sessions(self, sessions: List[Dict[str, Any]]):
        """设置会话列表"""
        self.sessions = sessions
        
    def render(self) -> Panel:
        """渲染会话面板"""
        if not self.sessions:
            return Panel(
                Align.center(Text("暂无会话", style="dim")),
                title="[bold]会话[/bold]",
                box=ROUNDED,
                style="cyan"
            )
        
        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("ID", style="dim", width=12)
        table.add_column("名称", style="cyan")
        table.add_column("类型", style="blue", width=10)
        table.add_column("消息数", style="green", width=8)
        table.add_column("更新时间", style="dim", width=16)
        
        for i, session in enumerate(self.sessions[:20]):  # 只显示前20个
            session_id = session.get("id", "")[:8]
            name = session.get("name", "未命名")
            session_type = session.get("type", "unknown")
            message_count = len(session.get("messages", []))
            updated_at = session.get("updated_at", "")
            
            # 格式化时间
            if updated_at:
                try:
                    dt = datetime.fromisoformat(updated_at)
                    updated_at = dt.strftime("%m-%d %H:%M")
                except:
                    pass
            
            if i == self.selected_index:
                table.add_row(
                    Text(session_id, style="bold"),
                    Text(name, style="bold white"),
                    session_type,
                    str(message_count),
                    updated_at
                )
            else:
                table.add_row(session_id, name, session_type, str(message_count), updated_at)
        
        return Panel(
            table,
            title=f"[bold]会话列表 ({len(self.sessions)})[/bold]",
            box=ROUNDED,
            style="cyan",
            padding=(1, 1)
        )


class ConfigPanel:
    """配置面板组件"""
    
    def __init__(self):
        self.configs: Dict[str, Any] = {}
        self.categories: List[str] = []
        self.selected_category = 0
        
    def set_configs(self, configs: Dict[str, Any]):
        """设置配置数据"""
        self.configs = configs
        self.categories = list(configs.keys())
        
    def render(self) -> Panel:
        """渲染配置面板"""
        if not self.configs:
            return Panel(
                Align.center(Text("暂无配置", style="dim")),
                title="[bold]配置[/bold]",
                box=ROUNDED,
                style="cyan"
            )
        
        # 创建分类标签
        tabs = Text()
        for i, cat in enumerate(self.categories):
            if i == self.selected_category:
                tabs.append(f" [{cat}] ", style="bold white on cyan")
            else:
                tabs.append(f" {cat} ", style="dim")
        
        # 当前分类的配置项
        current_cat = self.categories[self.selected_category] if self.categories else ""
        config_items = self.configs.get(current_cat, {})
        
        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("配置项", style="cyan", width=25)
        table.add_column("值", style="white")
        
        for key, value in config_items.items():
            # 截断过长的值
            value_str = str(value)
            if len(value_str) > 50:
                value_str = value_str[:50] + "..."
            table.add_row(key, value_str)
        
        content = Group(tabs, Text(), table)
        
        return Panel(
            content,
            title="[bold]系统配置[/bold]",
            box=ROUNDED,
            style="cyan",
            padding=(1, 1)
        )


class LoadingSpinner:
    """加载动画组件"""
    
    def __init__(self, message: str = "加载中..."):
        self.message = message
        self.progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            transient=True,
        )
        self.task = None
        
    def start(self):
        """开始加载动画"""
        self.task = self.progress.add_task(self.message, total=None)
        return self.progress
        
    def stop(self):
        """停止加载动画"""
        if self.task is not None:
            self.progress.stop()


class HelpPanel:
    """帮助面板组件"""
    
    def __init__(self):
        self.commands = {
            "/help": "显示帮助信息",
            "/quit": "退出CLI",
            "/clear": "清空屏幕",
            "/sessions": "查看会话列表",
            "/tools": "查看可用工具",
            "/config": "查看配置",
            "/chat": "进入聊天模式",
            "/back": "返回上一级",
        }
        
    def render(self) -> Panel:
        """渲染帮助面板"""
        table = Table(show_header=True, header_style="bold cyan", box=ROUNDED)
        table.add_column("命令", style="cyan", width=15)
        table.add_column("说明", style="white")
        
        for cmd, desc in self.commands.items():
            table.add_row(cmd, desc)
        
        return Panel(
            table,
            title="[bold]可用命令[/bold]",
            box=ROUNDED,
            style="cyan",
            padding=(1, 1)
        )
