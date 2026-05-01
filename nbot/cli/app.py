"""
CLI 应用程序主类 - 实现动态界面和屏幕切换
"""

import os
import sys
import time
from typing import Dict, Optional

try:
    from rich.console import Console
    from rich.live import Live
    from rich.layout import Layout
    from rich.text import Text
    from rich.panel import Panel
    from rich.align import Align
except ImportError:
    print("错误: 需要安装 rich 库")
    print("请运行: pip install rich")
    sys.exit(1)

# 导入屏幕类
from .screens import (
    BaseScreen,
    MainScreen,
    ChatScreen,
    ToolsScreen,
    SessionsScreen,
    ConfigScreen,
    HelpScreen,
)


class CLIApp:
    """CLI 应用程序主类"""
    
    def __init__(self):
        self.console = Console()
        self.screens: Dict[str, BaseScreen] = {}
        self.current_screen: Optional[BaseScreen] = None
        self.running = False
        self.live: Optional[Live] = None
        self.refresh_rate = 4  # 刷新率 (每秒)
        self.last_key = None
        self.input_buffer = ""
        
        # 初始化屏幕
        self._init_screens()
        
    def _init_screens(self):
        """初始化所有屏幕"""
        self.screens = {
            "main": MainScreen(self),
            "chat": ChatScreen(self),
            "tools": ToolsScreen(self),
            "sessions": SessionsScreen(self),
            "config": ConfigScreen(self),
            "help": HelpScreen(self),
        }
        self.current_screen = self.screens["main"]
        
    def switch_screen(self, screen_name: str):
        """切换到指定屏幕"""
        if screen_name in self.screens:
            self.current_screen = self.screens[screen_name]
            self.current_screen.update()
            
    def show_help(self):
        """显示帮助"""
        self.switch_screen("help")
        
    def _get_key(self) -> Optional[str]:
        """获取键盘输入 (跨平台)"""
        try:
            import msvcrt  # Windows
            if msvcrt.kbhit():
                key = msvcrt.getch()
                # 解码字节
                if key == b'\x00' or key == b'\xe0':
                    # 特殊键（方向键等）
                    key = msvcrt.getch()
                    key_map = {
                        b'H': 'up',
                        b'P': 'down',
                        b'K': 'left',
                        b'M': 'right',
                        b'G': 'home',
                        b'O': 'end',
                        b'S': 'delete',
                        b'I': 'pageup',
                        b'Q': 'pagedown',
                    }
                    return key_map.get(key, None)
                elif key == b'\x1b':
                    return 'escape'
                elif key == b'\r' or key == b'\n':
                    return 'enter'
                elif key == b'\x08':
                    return 'backspace'
                elif key == b'\t':
                    return 'tab'
                elif key == b'\x03':  # Ctrl+C
                    return 'ctrl_c'
                else:
                    try:
                        return key.decode('utf-8')
                    except:
                        return None
            return None
        except ImportError:
            # Unix/Linux/Mac
            try:
                import termios
                import tty
                import select
                
                fd = sys.stdin.fileno()
                old_settings = termios.tcgetattr(fd)
                try:
                    tty.setcbreak(fd)
                    if select.select([sys.stdin], [], [], 0)[0]:
                        key = sys.stdin.read(1)
                        if key == '\x1b':
                            # ESC序列（方向键等）
                            seq = sys.stdin.read(2)
                            key_map = {
                                '[A': 'up',
                                '[B': 'down',
                                '[C': 'right',
                                '[D': 'left',
                                '[H': 'home',
                                '[F': 'end',
                                '[3~': 'delete',
                                '[5~': 'pageup',
                                '[6~': 'pagedown',
                            }
                            return key_map.get(seq, None)
                        elif key == '\r' or key == '\n':
                            return 'enter'
                        elif key == '\x7f':
                            return 'backspace'
                        elif key == '\t':
                            return 'tab'
                        elif key == '\x03':  # Ctrl+C
                            return 'ctrl_c'
                        else:
                            return key
                finally:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                return None
            except:
                return None
                
    def _render(self) -> Layout:
        """渲染当前屏幕"""
        if self.current_screen:
            return self.current_screen.render()
        return Layout(Panel("No screen"))
        
    def run(self):
        """运行CLI应用"""
        self.running = True
        
        try:
            with Live(
                self._render(),
                console=self.console,
                refresh_per_second=self.refresh_rate,
                screen=True,  # 使用备用屏幕缓冲区
            ) as live:
                self.live = live
                
                while self.running:
                    # 更新显示
                    live.update(self._render())
                    
                    # 处理输入
                    key = self._get_key()
                    if key:
                        if key == 'ctrl_c':
                            self.running = False
                            break
                            
                        # 将输入传递给当前屏幕处理
                        if self.current_screen:
                            result = self.current_screen.handle_input(key)
                            if not result:
                                self.running = False
                                break
                                
                    # 小延迟避免CPU占用过高
                    time.sleep(0.05)
                    
        except KeyboardInterrupt:
            self.running = False
        except Exception as e:
            self.console.print(f"[red]错误: {e}[/red]")
        finally:
            self._cleanup()
            
    def _cleanup(self):
        """清理资源"""
        self.running = False
        if self.live:
            self.live.stop()
            
    def quit(self):
        """退出应用"""
        self.running = False


def main():
    """CLI入口函数"""
    # 确保数据目录存在
    os.makedirs(os.path.join("data", "web"), exist_ok=True)
    os.makedirs(os.path.join("data", "cli_sessions"), exist_ok=True)
    
    # 创建并运行应用
    app = CLIApp()
    app.run()


if __name__ == "__main__":
    main()
