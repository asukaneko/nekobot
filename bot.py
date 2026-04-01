import configparser
import importlib
import os
import threading

from dotenv import load_dotenv

# Skip ncatbot's GitHub proxy auto-detection during startup.
# None means "probe proxies"; empty string means "connect directly".
os.environ.setdefault("GITHUB_PROXY", "")

from ncatbot.utils.config import config as ncatbot_config
from ncatbot.utils.logger import get_log


def _sync_env_to_config_ini():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()

    config_parser = configparser.ConfigParser()
    config_parser.read("config.ini", encoding="utf-8")

    if "BotConfig" not in config_parser:
        config_parser["BotConfig"] = {}
    if "ApiKey" not in config_parser:
        config_parser["ApiKey"] = {}

    mapping = [
        ("BOT_UIN", "BotConfig", "bot_uin"),
        ("ROOT", "BotConfig", "root"),
        ("WS_URI", "BotConfig", "ws_uri"),
        ("TOKEN", "BotConfig", "token"),
        ("API_KEY", "ApiKey", "api_key"),
        ("BASE_URL", "ApiKey", "base_url"),
        ("MODEL", "ApiKey", "model"),
    ]
    for env_key, section, option in mapping:
        value = os.getenv(env_key)
        if value:
            config_parser.set(section, option, value)

    with open("config.ini", "w", encoding="utf-8") as f:
        config_parser.write(f)


_sync_env_to_config_ini()

_log = get_log()
_commands_module = None
web_server_instance = None
_pending_qq_bot = None


def _get_commands_module():
    global _commands_module
    if _commands_module is None:
        _commands_module = importlib.import_module("nbot.commands")
    return _commands_module


def _apply_env_to_ncatbot_config():
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


def _set_web_server_bot(bot):
    global web_server_instance, _pending_qq_bot
    _pending_qq_bot = bot
    if web_server_instance:
        web_server_instance.set_qq_bot(bot)
        _log.info("QQ Bot reference set in web server")


def start_web_server(host="0.0.0.0", port=5000, bot=None):
    global web_server_instance
    try:
        from nbot.web import create_web_app
        import logging

        _log.info(f"Starting Web Chat Server on {host}:{port}...")

        werkzeug_log = logging.getLogger("werkzeug")
        werkzeug_log.setLevel(logging.ERROR)
        werkzeug_log.disabled = True

        app, socketio, web_server = create_web_app()
        web_server_instance = web_server

        if bot:
            _set_web_server_bot(bot)
        elif _pending_qq_bot:
            _set_web_server_bot(_pending_qq_bot)

        socketio.run(
            app,
            host=host,
            port=port,
            debug=False,
            allow_unsafe_werkzeug=True,
            log_output=False,
        )
    except ImportError as e:
        _log.error(f"Failed to import web module: {e}")
        _log.info(
            "Install flask and flask-socketio to enable web chat: pip install flask flask-socketio"
        )
    except Exception as e:
        _log.error(f"Failed to start web server: {e}")


def run_bot():
    _log.info("Starting NekoBot QQ service...")
    commands = _get_commands_module()
    commands.bot.run(enable_webui_interaction=False)


if __name__ == "__main__":
    import sys

    _apply_env_to_ncatbot_config()

    web_disabled = "--no-web" in sys.argv
    only_web = "--only-web" in sys.argv
    web_port = 5000
    web_host = "0.0.0.0"

    for i, arg in enumerate(sys.argv):
        if arg == "--web-port" and i + 1 < len(sys.argv):
            web_port = int(sys.argv[i + 1])
        if arg == "--web-host" and i + 1 < len(sys.argv):
            web_host = sys.argv[i + 1]

    if web_disabled:
        _log.info("Starting NekoBot (Web disabled)...")
        run_bot()
    elif only_web:
        _log.info("Starting NekoBot Web Dashboard only (QQ disabled)...")
        start_web_server(host=web_host, port=web_port, bot=None)
    else:
        _log.info("Starting NekoBot with Web Dashboard...")
        web_thread = threading.Thread(
            target=start_web_server,
            args=(web_host, web_port, None),
            daemon=True,
        )
        web_thread.start()

        commands = _get_commands_module()
        _set_web_server_bot(commands.bot)
        commands.bot.run(enable_webui_interaction=False)
