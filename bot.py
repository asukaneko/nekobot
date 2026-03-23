from ncatbot.utils.logger import get_log
import nbot.commands
from nbot.chat import chat,tts,chat_video,chat_image,chat_webpage,chat_json,record_assistant_message,record_user_message,log_to_group_full_file,judge_reply,load_prompt,chat_gif
import os
import json
import datetime
import asyncio
import threading

_log = get_log()

try:
    if_tts = nbot.commands.if_tts
except AttributeError:
    if_tts = False

web_server_instance = None

def start_web_server(host='0.0.0.0', port=5000, bot=None):
    """启动 Web 服务"""
    global web_server_instance
    try:
        from nbot.web import create_web_app

        _log.info(f"Starting Web Chat Server on {host}:{port}...")

        app, socketio, web_server = create_web_app()
        web_server_instance = web_server
        
        # 设置 QQ Bot 引用
        if bot:
            web_server.set_qq_bot(bot)
            _log.info("QQ Bot reference set in web server")

        socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True)

    except ImportError as e:
        _log.error(f"Failed to import web module: {e}")
        _log.info("Install flask and flask-socketio to enable web chat: pip install flask flask-socketio")
    except Exception as e:
        _log.error(f"Failed to start web server: {e}")


def run_bot():
    """运行 QQ Bot"""
    _log.info("Starting NekoBot QQ service...")
    nbot.commands.bot.run(enable_webui_interaction=False)


if __name__ == '__main__':
    import sys

    # 解析命令行参数
    web_disabled = '--no-web' in sys.argv
    only_web = '--only-web' in sys.argv
    web_port = 5000
    web_host = '0.0.0.0'

    for i, arg in enumerate(sys.argv):
        if arg == '--web-port' and i + 1 < len(sys.argv):
            web_port = int(sys.argv[i + 1])
        if arg == '--web-host' and i + 1 < len(sys.argv):
            web_host = sys.argv[i + 1]

    if web_disabled:
        # 只启动 QQ Bot
        _log.info("Starting NekoBot (Web disabled)...")
        nbot.commands.bot.run(enable_webui_interaction=False)
    elif only_web:
        # 只启动 Web 服务（不连接 QQ）
        _log.info("Starting NekoBot Web Dashboard only (QQ disabled)...")
        start_web_server(host=web_host, port=web_port, bot=None)
    else:
        # 同时启动 QQ Bot 和 Web 服务
        _log.info("Starting NekoBot with Web Dashboard...")
        
        # 在主线程启动 Web 服务（传入 bot 引用）
        web_thread = threading.Thread(target=start_web_server, args=(web_host, web_port, nbot.commands.bot), daemon=True)
        web_thread.start()
        
        # 在主线程启动 QQ Bot
        nbot.commands.bot.run(enable_webui_interaction=False)
