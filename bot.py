import importlib
import os
import threading

from dotenv import load_dotenv

# Skip ncatbot's GitHub proxy auto-detection during startup.
# None means "probe proxies"; empty string means "connect directly".
os.environ.setdefault("GITHUB_PROXY", "")

from ncatbot.utils.config import config as ncatbot_config
from ncatbot.utils.logger import get_log


def _load_env_file():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()


_load_env_file()


def _apply_runtime_ncatbot_config():
    bot_uin = os.getenv("BOT_UIN")
    if bot_uin:
        ncatbot_config.set_bot_uin(bot_uin)

    root = os.getenv("ROOT")
    if root:
        ncatbot_config.set_root(root)

    ws_uri = os.getenv("WS_URI")
    if ws_uri:
        ncatbot_config.set_ws_uri(ws_uri)

    token = os.getenv("TOKEN")
    if token:
        ncatbot_config.set_token(token)

    webui_uri = os.getenv("WEBUI_URI")
    if webui_uri:
        ncatbot_config.set_webui_uri(webui_uri)


_apply_runtime_ncatbot_config()

_log = get_log()
_commands_module = None
web_server_instance = None
_pending_qq_bot = None
_web_server_started = threading.Event()


def _get_commands_module():
    global _commands_module
    if _commands_module is None:
        _commands_module = importlib.import_module("nbot.commands")
    return _commands_module


def _set_web_server_bot(bot):
    global web_server_instance, _pending_qq_bot
    _pending_qq_bot = bot
    if web_server_instance:
        web_server_instance.set_qq_bot(bot)
        _log.info("QQ Bot reference set in web server")


def _prepare_web_server(bot=None):
    global web_server_instance
    from nbot.web import create_web_app
    import logging

    werkzeug_log = logging.getLogger("werkzeug")
    werkzeug_log.setLevel(logging.ERROR)
    werkzeug_log.disabled = True

    app, socketio, web_server = create_web_app()
    web_server_instance = web_server

    if bot:
        _set_web_server_bot(bot)
    elif _pending_qq_bot:
        _set_web_server_bot(_pending_qq_bot)

    return app, socketio, web_server


def start_web_server(host="0.0.0.0", port=5000, bot=None, prepared=None):
    global web_server_instance
    try:
        _log.info(f"Starting Web Chat Server on {host}:{port}...")
        if prepared is None:
            app, socketio, web_server = _prepare_web_server(bot=bot)
        else:
            app, socketio, web_server = prepared
            web_server_instance = web_server
            if bot:
                _set_web_server_bot(bot)
            elif _pending_qq_bot:
                _set_web_server_bot(_pending_qq_bot)
        _web_server_started.set()
        socketio.run(
            app,
            host=host,
            port=port,
            debug=False,
            allow_unsafe_werkzeug=True,
            log_output=False,
        )
    except ImportError as e:
        _web_server_started.set()
        _log.error(f"Failed to import web module: {e}")
        _log.info(
            "Install flask and flask-socketio to enable web chat: pip install flask flask-socketio"
        )
    except Exception as e:
        _web_server_started.set()
        _log.error(f"Failed to start web server: {e}")


def run_bot():
    _log.info("Starting NekoBot QQ service...")
    commands = _get_commands_module()
    _set_web_server_bot(commands.bot)
    commands.bot.run(enable_webui_interaction=False)


def run_cli():
    """启动CLI模式"""
    _log.info("Starting NekoBot CLI mode...")
    try:
        from nbot.cli_cc_style import CCStyleCLI
        cli = CCStyleCLI()
        cli.run()
    except ImportError as e:
        _log.error(f"Failed to import CLI module: {e}")
        _log.info("Install rich to enable CLI: pip install rich")
        print("\n错误: 需要安装 rich 库")
        print("请运行: pip install rich")
    except Exception as e:
        _log.error(f"CLI error: {e}")
        print(f"\nCLI错误: {e}")


def run_cli_and_web(host="0.0.0.0", port=5000):
    """同时启动 CLI 和 Web"""
    _log.info("Starting NekoBot CLI and Web Dashboard...")
    
    # 准备 Web 服务器
    prepared = _prepare_web_server(bot=None)
    
    # 启动 Web 服务器线程
    web_thread = threading.Thread(
        target=start_web_server,
        args=(host, port, None, prepared),
        name="web-server",
        daemon=True,
    )
    web_thread.start()
    
    # 启动 CLI（在主线程）
    run_cli()


def _has_qq_bot_config():
    """检查是否配置了QQ机器人（ncatbot/napcat）"""
    bot_uin = os.getenv("BOT_UIN", "").strip()
    ws_uri = os.getenv("WS_URI", "").strip()
    
    # 如果环境变量没有配置，尝试从config.ini读取
    if not bot_uin or not ws_uri:
        try:
            import configparser
            config_parser = configparser.ConfigParser()
            config_parser.read("config.ini", encoding="utf-8")
            bot_uin = config_parser.get("BotConfig", "bot_uin", fallback="").strip()
            ws_uri = config_parser.get("BotConfig", "ws_uri", fallback="").strip()
        except Exception:
            pass
    
    # 检查配置是否有效（bot_uin是QQ号，ws_uri是websocket地址）
    has_bot_uin = bool(bot_uin and bot_uin not in ["", "0"])
    has_ws_uri = bool(ws_uri and ws_uri not in ["", "ws://", "ws://localhost"])
    
    return has_bot_uin and has_ws_uri


if __name__ == "__main__":
    import sys

    web_disabled = "--no-web" in sys.argv
    only_web = "--only-web" in sys.argv
    cli_mode = "--cli" in sys.argv
    cli_and_web = "--cli-and-web" in sys.argv
    web_port = 5000
    web_host = "0.0.0.0"

    for i, arg in enumerate(sys.argv):
        if arg == "--web-port" and i + 1 < len(sys.argv):
            web_port = int(sys.argv[i + 1])
        if arg == "--web-host" and i + 1 < len(sys.argv):
            web_host = sys.argv[i + 1]

    if cli_and_web:
        # CLI + Web 模式 - 同时启动命令行和 Web 界面
        run_cli_and_web(host=web_host, port=web_port)
    elif cli_mode:
        # CLI模式 - 启动命令行界面
        run_cli()
    elif web_disabled:
        _log.info("Starting NekoBot (Web disabled)...")
        run_bot()
    elif only_web:
        _log.info("Starting NekoBot Web Dashboard only (QQ disabled)...")
        prepared = _prepare_web_server(bot=None)
        start_web_server(host=web_host, port=web_port, bot=None, prepared=prepared)
    else:
        # 检查是否配置了QQ机器人
        if _has_qq_bot_config():
            _log.info("Starting NekoBot with Web Dashboard...")
            prepared = _prepare_web_server(bot=None)
            bot_thread = threading.Thread(
                target=run_bot,
                name="qq-bot-main",
                daemon=True,
            )
            bot_thread.start()
            start_web_server(host=web_host, port=web_port, bot=None, prepared=prepared)
        else:
            _log.info("No QQ bot config found, starting Web Dashboard only...")
            _log.info("To enable QQ bot, set BOT_UIN and WS_URI in .env or config.ini")
            prepared = _prepare_web_server(bot=None)
            start_web_server(host=web_host, port=web_port, bot=None, prepared=prepared)
