import configparser
import os
from dotenv import load_dotenv

def _sync_env_to_config_ini():
    _env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
    else:
        load_dotenv()

    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    if 'BotConfig' not in config_parser:
        config_parser['BotConfig'] = {}
    if 'ApiKey' not in config_parser:
        config_parser['ApiKey'] = {}

    bot_uin = os.getenv('BOT_UIN')
    if bot_uin:
        config_parser.set('BotConfig', 'bot_uin', bot_uin)
    root = os.getenv('ROOT')
    if root:
        config_parser.set('BotConfig', 'root', root)
    ws_uri = os.getenv('WS_URI')
    if ws_uri:
        config_parser.set('BotConfig', 'ws_uri', ws_uri)
    token = os.getenv('TOKEN')
    if token:
        config_parser.set('BotConfig', 'token', token)
    api_key = os.getenv('API_KEY')
    if api_key:
        config_parser.set('ApiKey', 'api_key', api_key)
    base_url = os.getenv('BASE_URL')
    if base_url:
        config_parser.set('ApiKey', 'base_url', base_url)
    model = os.getenv('MODEL')
    if model:
        config_parser.set('ApiKey', 'model', model)

    with open('config.ini', 'w', encoding='utf-8') as f:
        config_parser.write(f)

_sync_env_to_config_ini()

from ncatbot.utils.logger import get_log
from ncatbot.utils.config import config as ncatbot_config
import nbot.commands
from nbot.chat import chat,tts,chat_video,chat_image,chat_webpage,chat_json,record_assistant_message,record_user_message,log_to_group_full_file,judge_reply,load_prompt,chat_gif
import json
import datetime
import asyncio
import threading

_log = get_log()

try:
    if_tts = nbot.commands.if_tts
except AttributeError:
    if_tts = False

def _apply_env_to_ncatbot_config():
    bot_uin = os.getenv('BOT_UIN')
    if bot_uin:
        ncatbot_config.set_bot_uin(bot_uin)
    root = os.getenv('ROOT')
    if root:
        ncatbot_config.set_root(root)
    ws_uri = os.getenv('WS_URI')
    if ws_uri:
        ncatbot_config.set_ws_uri(ws_uri)
    token = os.getenv('TOKEN')
    if token:
        ncatbot_config.set_token(token)

web_server_instance = None

def start_web_server(host='0.0.0.0', port=5000, bot=None):
    """启动 Web 服务"""
    global web_server_instance
    try:
        from nbot.web import create_web_app

        _log.info(f"Starting Web Chat Server on {host}:{port}...")

        # 隐藏 Flask/Werkzeug 默认日志
        import logging
        werkzeug_log = logging.getLogger('werkzeug')
        werkzeug_log.setLevel(logging.ERROR)  # 只显示错误日志，隐藏 INFO 日志
        werkzeug_log.disabled = True  # 完全禁用 werkzeug 日志

        app, socketio, web_server = create_web_app()
        web_server_instance = web_server
        
        # 设置 QQ Bot 引用
        if bot:
            web_server.set_qq_bot(bot)
            _log.info("QQ Bot reference set in web server")

        socketio.run(app, host=host, port=port, debug=False, allow_unsafe_werkzeug=True, log_output=False)

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

    _apply_env_to_ncatbot_config()

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
