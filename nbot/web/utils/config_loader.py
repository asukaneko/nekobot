import configparser
import os
from dotenv import load_dotenv
from ncatbot.utils.config import config

# 尝试加载 .env 文件
load_dotenv()

def load_config():
    """加载配置，支持环境变量和 config.ini"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取，环境变量没有则从 config.ini 读取
    # Bot 配置
    bot_uin = os.getenv('BOT_UIN') or config_parser.get('BotConfig', 'bot_uin', fallback='')
    root = os.getenv('ROOT') or config_parser.get('BotConfig', 'root', fallback='')
    ws_uri = os.getenv('WS_URI') or config_parser.get('BotConfig', 'ws_uri', fallback="ws://localhost:3001")
    token = os.getenv('TOKEN') or config_parser.get('BotConfig', 'token', fallback="")
    webui_uri = os.getenv('WEBUI_URI') or config_parser.get('BotConfig', 'webui_uri', fallback="127.0.0.1:6099")

    config.set_bot_uin(bot_uin)
    config.set_root(root)
    config.set_ws_uri(ws_uri)
    config.set_token(token)
    config.set_webui_uri(webui_uri)

    return bot_uin, root


def get_api_config():
    """获取 API 配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    api_key = os.getenv('API_KEY') or config_parser.get('ApiKey', 'api_key', fallback='')
    base_url = os.getenv('BASE_URL') or config_parser.get('ApiKey', 'base_url', fallback='https://api.minimaxi.com/v1/text/chatcompletion_v2')
    model = os.getenv('MODEL') or config_parser.get('ApiKey', 'model', fallback='MiniMax-M2.7')
    silicon_api_key = os.getenv('SILICON_API_KEY') or config_parser.get('ApiKey', 'silicon_api_key', fallback='')

    return {
        'api_key': api_key,
        'base_url': base_url,
        'model': model,
        'silicon_api_key': silicon_api_key
    }


def get_web_config():
    """获取 Web 配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    password = os.getenv('WEB_PASSWORD') or config_parser.get('web', 'password', fallback='')

    return {
        'password': password
    }


def get_chat_config():
    """获取聊天配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    max_history_length = os.getenv('MAX_HISTORY_LENGTH') or config_parser.get('chat', 'MAX_HISTORY_LENGTH', fallback='50')

    return {
        'max_history_length': int(max_history_length)
    }


def get_pic_config():
    """获取图片识别配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    model = os.getenv('PIC_MODEL') or config_parser.get('pic', 'model', fallback='zai-org/GLM-4.6V')

    return {
        'model': model
    }


def get_cache_config():
    """获取缓存配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    cache_address = os.getenv('CACHE_ADDRESS') or config_parser.get('cache', 'cache_address', fallback='./cache')

    return {
        'cache_address': cache_address
    }


def get_voice_config():
    """获取语音配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    voice = os.getenv('VOICE') or config_parser.get('voice', 'voice', fallback='fnlp/MOSS-TTSD-v0.5:diana')

    return {
        'voice': voice
    }


def get_search_config():
    """获取搜索配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    api_key = os.getenv('SEARCH_API_KEY') or config_parser.get('search', 'api_key', fallback='')
    api_url = os.getenv('SEARCH_API_URL') or config_parser.get('search', 'api_url', fallback='')

    return {
        'api_key': api_key,
        'api_url': api_url
    }


def get_video_config():
    """获取视频配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    api_key = os.getenv('VIDEO_API_KEY') or config_parser.get('video', 'api_key', fallback='')

    return {
        'api_key': api_key
    }


def get_gf_config():
    """获取 GitHub 配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    api_key = os.getenv('GF_API_KEY') or config_parser.get('gf', 'api_key', fallback='')

    return {
        'api_key': api_key
    }


def get_pdf_config():
    """获取 PDF 配置"""
    config_parser = configparser.ConfigParser()
    config_parser.read('config.ini', encoding='utf-8')

    # 优先从环境变量读取
    api_key = os.getenv('PDF_API_KEY') or config_parser.get('pdf', 'api_key', fallback='')

    return {
        'api_key': api_key
    }
