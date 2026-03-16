"""
工具模块 - 提供 AI 可调用的各种工具
"""
import json
import logging
import urllib.request
import urllib.parse
import os
from typing import Dict, Any, Optional, List
from datetime import datetime
import configparser

_log = logging.getLogger(__name__)

# 加载配置文件
config_parser = configparser.ConfigParser()
config_parser.read('config.ini', encoding='utf-8')

# 读取MiniMax API配置（用于web search）
MINIMAX_API_KEY = config_parser.get('ApiKey', 'api_key', fallback="")
MINIMAX_BASE_URL = config_parser.get('ApiKey', 'base_url', fallback="https://api.minimaxi.com/v1/text/chatcompletion_v2")
MINIMAX_MODEL = config_parser.get('ApiKey', 'model', fallback="MiniMax-Text-01")

# Web 配置数据目录
WEB_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'web')


def load_tools_config() -> List[Dict]:
    """从 web 配置文件加载 tools 配置"""
    tools_file = os.path.join(WEB_DATA_DIR, 'tools.json')
    if os.path.exists(tools_file):
        try:
            with open(tools_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            _log.error(f"Failed to load tools config: {e}")
    return []


def get_enabled_tools() -> List[Dict]:
    """获取启用的工具列表（从web配置）"""
    web_config = load_tools_config()
    if web_config:
        # 获取启用的工具名称列表
        enabled_names = {t['name'] for t in web_config if t.get('enabled', True)}
        # 过滤 TOOL_DEFINITIONS
        return [t for t in TOOL_DEFINITIONS if t['function']['name'] in enabled_names]
    return TOOL_DEFINITIONS  # 默认返回所有工具


class ToolExecutor:
    """工具执行器"""

    @staticmethod
    def search_news(query: str = "热点新闻", count: int = 5) -> Dict[str, Any]:
        """
        搜索新闻
        使用免费的 NewsAPI 或 RSS 源获取新闻
        """
        try:
            # 这里使用一个简单的新闻 API 示例
            # 实际使用时可以替换为真实的新闻 API，如 NewsAPI、Bing News API 等
            # 或者使用 RSS 源抓取

            # 示例：使用 RSS 源获取新闻
            import feedparser

            # 使用一些常见的 RSS 新闻源
            rss_urls = [
                "https://news.google.com/rss?hl=zh-CN&gl=CN&ceid=CN:zh",
                "https://rsshub.app/163/news/special/1",  # 网易新闻
            ]

            news_list = []
            for url in rss_urls[:1]:  # 先使用第一个源
                try:
                    feed = feedparser.parse(url)
                    for entry in feed.entries[:count]:
                        news_list.append({
                            "title": entry.get("title", ""),
                            "summary": entry.get("summary", entry.get("description", ""))[:200],
                            "link": entry.get("link", ""),
                            "published": entry.get("published", "")
                        })
                except Exception as e:
                    _log.error(f"Failed to parse RSS {url}: {e}")
                    continue

            if not news_list:
                # 如果 RSS 获取失败，返回模拟数据
                return {
                    "success": True,
                    "query": query,
                    "news": [
                        {
                            "title": f"关于 '{query}' 的最新动态",
                            "summary": "由于新闻 API 限制，暂时无法获取实时新闻。建议配置 NewsAPI 或其他新闻源。",
                            "link": "",
                            "published": datetime.now().isoformat()
                        }
                    ]
                }

            return {
                "success": True,
                "query": query,
                "news": news_list
            }

        except Exception as e:
            _log.error(f"Search news error: {e}")
            return {
                "success": False,
                "error": str(e),
                "query": query
            }

    @staticmethod
    def get_weather(city: str = "北京") -> Dict[str, Any]:
        """
        查询天气
        使用免费的天气 API
        """
        try:
            # 使用 wttr.in 免费天气服务
            url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"

            req = urllib.request.Request(
                url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0'
                }
            )

            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode('utf-8'))

            current = data.get('current_condition', [{}])[0]
            weather = {
                "city": city,
                "temperature": current.get('temp_C', 'N/A'),
                "feels_like": current.get('FeelsLikeC', 'N/A'),
                "description": current.get('lang_zh', [{}])[0].get('value', current.get('weatherDesc', [{}])[0].get('value', 'N/A')),
                "humidity": current.get('humidity', 'N/A'),
                "wind": current.get('windspeedKmph', 'N/A'),
                "observation_time": current.get('observation_time', 'N/A')
            }

            return {
                "success": True,
                "weather": weather
            }

        except Exception as e:
            _log.error(f"Get weather error: {e}")
            return {
                "success": False,
                "error": str(e),
                "city": city
            }

    @staticmethod
    def search_web(query: str, num_results: int = 3) -> Dict[str, Any]:
        """
        网页搜索
        使用 MiniMax API 的 web_search 工具进行搜索
        """
        try:
            # 检查配置是否完整
            if not MINIMAX_API_KEY:
                _log.error("MiniMax API密钥未配置，请在config.ini中配置[ApiKey]部分的api_key")
                return {
                    "success": False,
                    "error": "MiniMax API密钥未配置",
                    "query": query
                }

            # 构建请求
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MINIMAX_API_KEY}"
            }
            
            # 构建消息
            messages = [
                {
                    "role": "system",
                    "content": "你是一个 helpful assistant，可以使用网络搜索工具来获取最新信息。"
                },
                {
                    "role": "user",
                    "content": query
                }
            ]
            
            # 构建payload，启用web_search工具
            payload = {
                "model": MINIMAX_MODEL,
                "messages": messages,
                "tools": [
                    {
                        "type": "web_search"
                    }
                ],
                "max_tokens": 4096
            }

            # 发送请求
            import requests
            url = MINIMAX_BASE_URL
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()

            # 解析响应
            result = response.json()
            
            # 检查是否有搜索结果
            choices = result.get("choices", [])
            if not choices:
                return {
                    "success": False,
                    "error": "API返回结果为空",
                    "query": query
                }
            
            # 获取消息列表
            messages_list = choices[0].get("messages", [])
            
            # 查找tool结果和最终回复
            search_content = ""
            final_content = ""
            
            for msg in messages_list:
                if msg.get("role") == "tool":
                    search_content = msg.get("content", "")
                elif msg.get("role") == "assistant" and not msg.get("tool_calls"):
                    final_content = msg.get("content", "")
            
            # 如果没有找到结构化结果，尝试直接获取content
            if not final_content and choices[0].get("message"):
                final_content = choices[0].get("message", {}).get("content", "")
            
            # 解析搜索结果
            formatted_results = []
            if search_content:
                # 尝试从搜索结果中提取网页信息
                # MiniMax返回的格式通常包含参考资料
                import re
                
                # 提取URL和标题（如果有的话）
                url_pattern = r'https?://[^\s\]]+'
                urls = re.findall(url_pattern, search_content)
                
                if urls:
                    for i, url in enumerate(urls[:num_results]):
                        formatted_results.append({
                            "title": f"搜索结果 {i+1}",
                            "snippet": search_content[:300] if i == 0 else "",
                            "url": url
                        })
                else:
                    formatted_results.append({
                        "title": "搜索结果",
                        "snippet": search_content[:500],
                        "url": ""
                    })
            
            return {
                "success": True,
                "query": query,
                "results": formatted_results,
                "answer": final_content
            }

        except requests.exceptions.RequestException as e:
            _log.error(f"Web search request error: {e}")
            return {
                "success": False,
                "error": f"搜索请求失败: {str(e)}",
                "query": query
            }
        except Exception as e:
            _log.error(f"Web search error: {e}")
            return {
                "success": False,
                "error": str(e),
                "query": query
            }

    @staticmethod
    def get_date_time() -> Dict[str, Any]:
        """获取当前日期和时间"""
        now = datetime.now()
        return {
            "success": True,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": now.strftime("%A"),
            "weekday_cn": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][now.weekday()],
            "timestamp": now.isoformat()
        }

    @staticmethod
    def http_get(url: str) -> Dict[str, Any]:
        """HTTP GET 请求"""
        try:
            req = urllib.request.Request(
                url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            )

            with urllib.request.urlopen(req, timeout=30) as response:
                content = response.read().decode('utf-8', errors='ignore')
                return {
                    "success": True,
                    "url": url,
                    "status": response.status,
                    "content": content[:5000]  # 限制返回内容长度
                }

        except Exception as e:
            _log.error(f"HTTP GET error: {e}")
            return {
                "success": False,
                "error": str(e),
                "url": url
            }


# 工具定义（用于 AI 工具调用）
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": "搜索最新新闻。当用户需要获取新闻资讯时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，如'科技'、'体育'、'财经'等，默认为'热点新闻'"
                    },
                    "count": {
                        "type": "integer",
                        "description": "返回的新闻数量，默认5条",
                        "default": 5
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的天气信息。当用户询问天气时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，如'北京'、'上海'、'广州'等，默认'北京'"
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": "搜索网页内容。当需要查询网络信息时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词"
                    },
                    "num_results": {
                        "type": "integer",
                        "description": "返回结果数量，默认3条",
                        "default": 3
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_date_time",
            "description": "获取当前日期和时间信息。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "http_get",
            "description": "发送 HTTP GET 请求获取网页内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要访问的 URL 地址"
                    }
                },
                "required": ["url"]
            }
        }
    }
]


def execute_tool(tool_name: str, arguments: Dict[str, Any], context: Dict = None) -> Dict[str, Any]:
    """
    执行指定的工具（优先使用 Web 配置）

    Args:
        tool_name: 工具名称
        arguments: 工具参数
        context: 可选的上下文信息

    Returns:
        工具执行结果
    """
    # 1. 先从 Web 配置查找
    web_config = load_tools_config()
    tool_config = None

    for config in web_config:
        if config.get('name') == tool_name:
            tool_config = config
            break

    # 2. 如果找到 Web 配置且有 implementation，使用动态执行
    if tool_config and tool_config.get('implementation'):
        _log.info(f"Executing dynamic tool: {tool_name}")
        executor = get_executor()
        return executor.execute_tool(tool_config, arguments, context)

    # 3. 否则使用内置 Tool
    _log.info(f"Executing built-in tool: {tool_name}")
    executor = ToolExecutor()

    tool_map = {
        "search_news": executor.search_news,
        "get_weather": executor.get_weather,
        "search_web": executor.search_web,
        "get_date_time": executor.get_date_time,
        "http_get": executor.http_get,
    }

    if tool_name not in tool_map:
        return {
            "success": False,
            "error": f"Unknown tool: {tool_name}"
        }

    try:
        tool_func = tool_map[tool_name]
        result = tool_func(**arguments)
        return result
    except Exception as e:
        _log.error(f"Tool execution error: {e}")
        return {
            "success": False,
            "error": str(e)
        }
