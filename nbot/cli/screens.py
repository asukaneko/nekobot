"""
CLI 屏幕/界面模块 - 实现多级界面切换
"""

import os
import sys
import json
import asyncio
from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional, Callable
from datetime import datetime

from rich.console import Console, Group
from rich.panel import Panel
from rich.text import Text
from rich.table import Table
from rich.layout import Layout
from rich.align import Align
from rich import box

from .components import (
    Header, Footer, Sidebar, MessagePanel, InputBox,
    ToolPanel, SessionPanel, ConfigPanel, HelpPanel,
    Message, LoadingSpinner
)


class BaseScreen(ABC):
    """屏幕基类"""
    
    def __init__(self, app: 'CLIApp'):
        self.app = app
        self.console = Console()
        self.header = Header()
        self.footer = Footer()
        self.layout = Layout()
        self.name = "base"
        
    @abstractmethod
    def build_layout(self) -> Layout:
        """构建布局"""
        pass
        
    @abstractmethod
    def handle_input(self, key: str) -> bool:
        """处理输入，返回是否继续运行"""
        pass
        
    @abstractmethod
    def update(self):
        """更新屏幕内容"""
        pass
        
    def render(self) -> Layout:
        """渲染屏幕"""
        return self.build_layout()


class MainScreen(BaseScreen):
    """主屏幕 - 欢迎界面和导航"""
    
    def __init__(self, app: 'CLIApp'):
        super().__init__(app)
        self.name = "main"
        self.sidebar = Sidebar(width=35)
        self.help_panel = HelpPanel()
        self.info_panel = None
        
        # 设置侧边栏项目
        self.sidebar.set_items([
            {"icon": "💬", "label": "聊天", "description": "开始新的对话", "action": "chat"},
            {"icon": "📜", "label": "会话", "description": "查看历史会话", "action": "sessions"},
            {"icon": "🔧", "label": "工具", "description": "管理工具", "action": "tools"},
            {"icon": "⚙️", "label": "配置", "description": "系统设置", "action": "config"},
            {"icon": "❓", "label": "帮助", "description": "查看帮助", "action": "help"},
            {"icon": "🚪", "label": "退出", "description": "退出CLI", "action": "quit"},
        ])
        
        self._load_system_info()
        
    def _load_system_info(self):
        """加载系统信息"""
        info = {
            "version": "1.0.0",
            "data_dir": "data/",
            "sessions_count": 0,
            "tools_count": 0,
        }
        
        # 加载会话数量
        sessions_file = os.path.join("data", "web", "sessions.json")
        if os.path.exists(sessions_file):
            try:
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions = json.load(f)
                    info["sessions_count"] = len(sessions)
            except:
                pass
                
        # 加载工具数量
        tools_file = os.path.join("data", "web", "tools.json")
        if os.path.exists(tools_file):
            try:
                with open(tools_file, 'r', encoding='utf-8') as f:
                    tools = json.load(f)
                    info["tools_count"] = len(tools)
            except:
                pass
        
        self.info = info
        
    def build_layout(self) -> Layout:
        """构建主界面布局"""
        layout = Layout()
        
        # 分割布局
        layout.split_column(
            Layout(self.header.render(self.name), size=3),
            Layout(name="body"),
            Layout(self.footer.render(), size=3)
        )
        
        # 主体部分分割为左右
        layout["body"].split_row(
            Layout(self.sidebar.render("导航"), size=35),
            Layout(name="content")
        )
        
        # 内容区域显示欢迎信息和快捷操作
        welcome_text = self._build_welcome_panel()
        layout["content"].update(welcome_text)
        
        return layout
        
    def _build_welcome_panel(self) -> Panel:
        """构建欢迎面板"""
        # ASCII Art Logo
        logo = Text()
        logo.append("""
    ╭─────────────────────────────────────╮
    │                                     │
    │   🐱 NekoBot CLI                    │
    │                                     │
    │   智能助手命令行界面                 │
    │                                     │
    ╰─────────────────────────────────────╯
        """, style="cyan")
        
        # 系统信息
        info_table = Table(show_header=False, box=box.SIMPLE)
        info_table.add_column("项目", style="cyan", width=15)
        info_table.add_column("值", style="white")
        info_table.add_row("版本", self.info.get("version", "unknown"))
        info_table.add_row("数据目录", self.info.get("data_dir", "data/"))
        info_table.add_row("会话数量", str(self.info.get("sessions_count", 0)))
        info_table.add_row("工具数量", str(self.info.get("tools_count", 0)))
        
        # 快捷提示
        tips = Text()
        tips.append("\n快捷操作:\n", style="bold cyan")
        tips.append("  ↑/↓  - 导航菜单\n", style="dim")
        tips.append("  Enter - 确认选择\n", style="dim")
        tips.append("  /help - 显示帮助\n", style="dim")
        tips.append("  Ctrl+C - 退出\n", style="dim")
        
        content = Group(logo, info_table, tips)
        
        return Panel(
            content,
            title="[bold]欢迎[/bold]",
            box=box.ROUNDED,
            style="cyan",
            padding=(1, 2)
        )
        
    def handle_input(self, key: str) -> bool:
        """处理输入"""
        if key == "up":
            self.sidebar.select_previous()
        elif key == "down":
            self.sidebar.select_next()
        elif key == "enter":
            selected = self.sidebar.get_selected()
            if selected:
                action = selected.get("action")
                if action == "quit":
                    return False
                elif action == "help":
                    self.app.show_help()
                else:
                    self.app.switch_screen(action)
        elif key == "/":
            self.app.show_help()
        elif key == "q":
            return False
            
        return True
        
    def update(self):
        """更新屏幕"""
        self._load_system_info()


class ChatScreen(BaseScreen):
    """聊天屏幕"""
    
    def __init__(self, app: 'CLIApp'):
        super().__init__(app)
        self.name = "chat"
        self.message_panel = MessagePanel()
        self.input_box = InputBox("输入消息，按Enter发送...")
        self.sidebar = Sidebar(width=25)
        self.current_session_id = None
        
        # 设置侧边栏项目
        self.sidebar.set_items([
            {"icon": "➕", "label": "新对话", "action": "new_chat"},
            {"icon": "📜", "label": "历史", "action": "history"},
            {"icon": "💾", "label": "保存", "action": "save"},
            {"icon": "🗑️", "label": "清空", "action": "clear"},
            {"icon": "🔙", "label": "返回", "action": "back"},
        ])
        
        # 加载历史会话或创建新会话
        self._init_session()
        
    def _init_session(self):
        """初始化会话"""
        # 尝试加载最新的会话
        sessions_file = os.path.join("data", "web", "sessions.json")
        if os.path.exists(sessions_file):
            try:
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions = json.load(f)
                    if sessions:
                        # 获取最新的会话
                        latest_session = max(sessions.values(), 
                                           key=lambda x: x.get('updated_at', ''))
                        self.current_session_id = latest_session.get('id')
                        self._load_session_messages(latest_session)
            except Exception as e:
                print(f"加载会话失败: {e}")
                
        if not self.current_session_id:
            self._create_new_session()
            
    def _create_new_session(self):
        """创建新会话"""
        import uuid
        self.current_session_id = str(uuid.uuid4())
        self.message_panel.clear()
        self.message_panel.add_message(Message(
            role="system",
            content="开始新的对话，我是NekoBot，有什么可以帮助你的吗？喵~"
        ))
        
    def _load_session_messages(self, session: Dict):
        """加载会话消息"""
        self.message_panel.clear()
        messages = session.get('messages', [])
        for msg in messages:
            self.message_panel.add_message(Message(
                role=msg.get('role', 'user'),
                content=msg.get('content', ''),
                timestamp=datetime.fromisoformat(msg.get('timestamp', datetime.now().isoformat()))
            ))
            
    def build_layout(self) -> Layout:
        """构建聊天界面布局"""
        layout = Layout()
        
        layout.split_column(
            Layout(self.header.render(self.name), size=3),
            Layout(name="body"),
            Layout(self.input_box.render(), size=3),
            Layout(self.footer.render(), size=3)
        )
        
        # 主体部分
        layout["body"].split_row(
            Layout(self.sidebar.render("操作"), size=25),
            Layout(self.message_panel.render())
        )
        
        return layout
        
    def handle_input(self, key: str) -> bool:
        """处理输入"""
        if key == "enter":
            content = self.input_box.get_content().strip()
            if content:
                if content.startswith("/"):
                    # 处理命令
                    return self._handle_command(content)
                else:
                    # 发送消息
                    self._send_message(content)
                self.input_box.clear()
        elif key == "backspace":
            self.input_box.backspace()
        elif key == "delete":
            self.input_box.delete()
        elif key == "left":
            self.input_box.move_cursor_left()
        elif key == "right":
            self.input_box.move_cursor_right()
        elif key == "up":
            self.sidebar.select_previous()
        elif key == "down":
            self.sidebar.select_next()
        elif key == "tab":
            selected = self.sidebar.get_selected()
            if selected:
                action = selected.get("action")
                if action == "back":
                    self.app.switch_screen("main")
                elif action == "new_chat":
                    self._create_new_session()
                elif action == "clear":
                    self.message_panel.clear()
                elif action == "save":
                    self._save_session()
        elif key == "escape":
            self.app.switch_screen("main")
        elif len(key) == 1:
            self.input_box.append_char(key)
            
        return True
        
    def _handle_command(self, command: str) -> bool:
        """处理命令"""
        cmd = command.lower().strip()
        
        if cmd == "/quit" or cmd == "/exit":
            return False
        elif cmd == "/back":
            self.app.switch_screen("main")
        elif cmd == "/clear":
            self.message_panel.clear()
        elif cmd == "/help":
            self.message_panel.add_message(Message(
                role="system",
                content="""可用命令：
/help - 显示帮助
/clear - 清空消息
/back - 返回主菜单
/quit - 退出CLI"""
            ))
        else:
            self.message_panel.add_message(Message(
                role="system",
                content=f"未知命令: {command}"
            ))
            
        return True
        
    def _send_message(self, content: str):
        """发送消息"""
        # 添加用户消息
        self.message_panel.add_message(Message(
            role="user",
            content=content
        ))
        
        # TODO: 调用AI服务获取回复
        # 这里先模拟一个回复
        self.message_panel.add_message(Message(
            role="assistant",
            content=f"收到你的消息: {content}\n\n(注意：AI回复功能需要配置API密钥才能使用)"
        ))
        
        # 保存会话
        self._save_session()
        
    def _save_session(self):
        """保存会话到文件"""
        try:
            sessions_file = os.path.join("data", "web", "sessions.json")
            sessions = {}
            
            if os.path.exists(sessions_file):
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions = json.load(f)
                    
            # 构建会话数据
            session_data = {
                "id": self.current_session_id,
                "name": "CLI会话",
                "type": "cli",
                "messages": [
                    {
                        "id": str(i),
                        "role": msg.role,
                        "content": msg.content,
                        "timestamp": msg.timestamp.isoformat(),
                        "sender": "user" if msg.role == "user" else "AI",
                        "source": "cli",
                        "session_id": self.current_session_id,
                    }
                    for i, msg in enumerate(self.message_panel.messages)
                ],
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            
            sessions[self.current_session_id] = session_data
            
            # 保存到文件
            os.makedirs(os.path.dirname(sessions_file), exist_ok=True)
            with open(sessions_file, 'w', encoding='utf-8') as f:
                json.dump(sessions, f, ensure_ascii=False, indent=2)
                
        except Exception as e:
            print(f"保存会话失败: {e}")
            
    def update(self):
        """更新屏幕"""
        pass


class ToolsScreen(BaseScreen):
    """工具管理屏幕"""
    
    def __init__(self, app: 'CLIApp'):
        super().__init__(app)
        self.name = "tools"
        self.tool_panel = ToolPanel()
        self.sidebar = Sidebar(width=25)
        
        # 设置侧边栏项目
        self.sidebar.set_items([
            {"icon": "✓", "label": "启用", "action": "enable"},
            {"icon": "✗", "label": "禁用", "action": "disable"},
            {"icon": "🔄", "label": "刷新", "action": "refresh"},
            {"icon": "🔙", "label": "返回", "action": "back"},
        ])
        
        self._load_tools()
        
    def _load_tools(self):
        """加载工具列表"""
        tools = []
        tools_file = os.path.join("data", "web", "tools.json")
        
        if os.path.exists(tools_file):
            try:
                with open(tools_file, 'r', encoding='utf-8') as f:
                    tools = json.load(f)
            except:
                pass
                
        self.tool_panel.set_tools(tools)
        
    def build_layout(self) -> Layout:
        """构建工具界面布局"""
        layout = Layout()
        
        layout.split_column(
            Layout(self.header.render(self.name), size=3),
            Layout(name="body"),
            Layout(self.footer.render(), size=3)
        )
        
        layout["body"].split_row(
            Layout(self.sidebar.render("操作"), size=25),
            Layout(self.tool_panel.render())
        )
        
        return layout
        
    def handle_input(self, key: str) -> bool:
        """处理输入"""
        if key == "up":
            self.sidebar.select_previous()
        elif key == "down":
            self.sidebar.select_next()
        elif key == "enter":
            selected = self.sidebar.get_selected()
            if selected:
                action = selected.get("action")
                if action == "back":
                    self.app.switch_screen("main")
                elif action == "refresh":
                    self._load_tools()
        elif key == "escape":
            self.app.switch_screen("main")
        elif key == "b":
            self.app.switch_screen("main")
            
        return True
        
    def update(self):
        """更新屏幕"""
        self._load_tools()


class SessionsScreen(BaseScreen):
    """会话列表屏幕"""
    
    def __init__(self, app: 'CLIApp'):
        super().__init__(app)
        self.name = "sessions"
        self.session_panel = SessionPanel()
        self.sidebar = Sidebar(width=25)
        
        # 设置侧边栏项目
        self.sidebar.set_items([
            {"icon": "📂", "label": "打开", "action": "open"},
            {"icon": "🗑️", "label": "删除", "action": "delete"},
            {"icon": "🔄", "label": "刷新", "action": "refresh"},
            {"icon": "🔙", "label": "返回", "action": "back"},
        ])
        
        self._load_sessions()
        
    def _load_sessions(self):
        """加载会话列表"""
        sessions = []
        sessions_file = os.path.join("data", "web", "sessions.json")
        
        if os.path.exists(sessions_file):
            try:
                with open(sessions_file, 'r', encoding='utf-8') as f:
                    sessions_data = json.load(f)
                    sessions = list(sessions_data.values())
            except:
                pass
                
        self.session_panel.set_sessions(sessions)
        
    def build_layout(self) -> Layout:
        """构建会话界面布局"""
        layout = Layout()
        
        layout.split_column(
            Layout(self.header.render(self.name), size=3),
            Layout(name="body"),
            Layout(self.footer.render(), size=3)
        )
        
        layout["body"].split_row(
            Layout(self.sidebar.render("操作"), size=25),
            Layout(self.session_panel.render())
        )
        
        return layout
        
    def handle_input(self, key: str) -> bool:
        """处理输入"""
        if key == "up":
            self.sidebar.select_previous()
        elif key == "down":
            self.sidebar.select_next()
        elif key == "enter":
            selected = self.sidebar.get_selected()
            if selected:
                action = selected.get("action")
                if action == "back":
                    self.app.switch_screen("main")
                elif action == "refresh":
                    self._load_sessions()
        elif key == "escape":
            self.app.switch_screen("main")
        elif key == "b":
            self.app.switch_screen("main")
            
        return True
        
    def update(self):
        """更新屏幕"""
        self._load_sessions()


class ConfigScreen(BaseScreen):
    """配置管理屏幕"""
    
    def __init__(self, app: 'CLIApp'):
        super().__init__(app)
        self.name = "config"
        self.config_panel = ConfigPanel()
        self.sidebar = Sidebar(width=25)
        
        # 设置侧边栏项目
        self.sidebar.set_items([
            {"icon": "💾", "label": "保存", "action": "save"},
            {"icon": "🔄", "label": "刷新", "action": "refresh"},
            {"icon": "🔙", "label": "返回", "action": "back"},
        ])
        
        self._load_configs()
        
    def _load_configs(self):
        """加载配置"""
        configs = {
            "AI": {},
            "系统": {},
            "工具": {},
        }
        
        # 加载AI配置
        ai_config_file = os.path.join("data", "web", "ai_config.json")
        if os.path.exists(ai_config_file):
            try:
                with open(ai_config_file, 'r', encoding='utf-8') as f:
                    configs["AI"] = json.load(f)
            except:
                pass
                
        # 加载系统配置
        settings_file = os.path.join("data", "web", "settings.json")
        if os.path.exists(settings_file):
            try:
                with open(settings_file, 'r', encoding='utf-8') as f:
                    configs["系统"] = json.load(f)
            except:
                pass
                
        self.config_panel.set_configs(configs)
        
    def build_layout(self) -> Layout:
        """构建配置界面布局"""
        layout = Layout()
        
        layout.split_column(
            Layout(self.header.render(self.name), size=3),
            Layout(name="body"),
            Layout(self.footer.render(), size=3)
        )
        
        layout["body"].split_row(
            Layout(self.sidebar.render("操作"), size=25),
            Layout(self.config_panel.render())
        )
        
        return layout
        
    def handle_input(self, key: str) -> bool:
        """处理输入"""
        if key == "up":
            self.sidebar.select_previous()
        elif key == "down":
            self.sidebar.select_next()
        elif key == "enter":
            selected = self.sidebar.get_selected()
            if selected:
                action = selected.get("action")
                if action == "back":
                    self.app.switch_screen("main")
                elif action == "refresh":
                    self._load_configs()
        elif key == "escape":
            self.app.switch_screen("main")
        elif key == "b":
            self.app.switch_screen("main")
            
        return True
        
    def update(self):
        """更新屏幕"""
        self._load_configs()


class HelpScreen(BaseScreen):
    """帮助屏幕"""
    
    def __init__(self, app: 'CLIApp'):
        super().__init__(app)
        self.name = "help"
        self.help_panel = HelpPanel()
        
    def build_layout(self) -> Layout:
        """构建帮助界面布局"""
        layout = Layout()
        
        layout.split_column(
            Layout(self.header.render(self.name), size=3),
            Layout(self.help_panel.render()),
            Layout(self.footer.render(), size=3)
        )
        
        return layout
        
    def handle_input(self, key: str) -> bool:
        """处理输入"""
        if key == "escape" or key == "q" or key == "enter":
            self.app.switch_screen("main")
            
        return True
        
    def update(self):
        """更新屏幕"""
        pass
