import configparser
from ncatbot.utils.config import config

def load_config():
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini')

    bot_uin = config_parser.get('BotConfig', 'bot_uin')
    root = config_parser.get('BotConfig', 'root')
    ws_uri = config_parser.get('BotConfig', 'ws_uri', fallback="ws://localhost:3001")
    token = config_parser.get('BotConfig', 'token', fallback="")

    config.set_bot_uin(bot_uin)
    config.set_root(root)
    config.set_ws_uri(ws_uri)
    config.set_token(token)

    return bot_uin,root
