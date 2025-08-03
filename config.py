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
    #config.set_webui_uri("xxxxxx:6099") #自定义webui地址，用于docker或者远程连接
    # 注意，如果使用远程连接，那么本地下载的文件应该要能被napcat服务器访问，否则出现无法找到文件的出错误
    return bot_uin,root
