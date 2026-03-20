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

# MiniMax API 配置（仅用于 search_web 和 understand_image 工具）
MINIMAX_API_KEY = config_parser.get('ApiKey', 'api_key', fallback="")

# 固定工具 API URL
MINIMAX_SEARCH_URL = "https://api.minimaxi.com/v1/coding_plan/search"
MINIMAX_VLM_URL = "https://api.minimaxi.com/v1/coding_plan/vlm"

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


def process_image_url(image_source: str) -> str:
    """
    处理图片源 - 转换本地文件为 base64 或直接使用 URL
    
    Args:
        image_source: 图片URL或本地文件路径
        
    Returns:
        处理后的图片URL或 base64 编码
    """
    # 如果是 HTTP/HTTPS URL，直接返回
    if image_source.startswith("http://") or image_source.startswith("https://"):
        return image_source
    
    # 移除 @ 前缀
    if image_source.startswith("@"):
        image_source = image_source[1:]
    
    # 读取本地文件并转换为 base64
    try:
        import base64
        with open(image_source, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
            return f"data:image/jpeg;base64,{image_data}"
    except Exception as e:
        raise ValueError(f"无法读取图片文件: {e}")


def get_enabled_tools() -> List[Dict]:
    """获取启用的工具列表
    
    自动合并所有工具类别，方便后续扩展新的工具类别
    """
    web_config = load_tools_config()
    enabled_names = set()
    if web_config:
        enabled_names = {t['name'] for t in web_config if t.get('enabled', True)}

    # 从配置启用工具
    if enabled_names:
        tools = [t for t in TOOL_DEFINITIONS if t['function']['name'] in enabled_names]
    else:
        tools = list(TOOL_DEFINITIONS)

    # 合并所有工具类别
    all_tool_categories = [
        WORKSPACE_TOOL_DEFINITIONS,
    ]
    
    for category in all_tool_categories:
        tools.extend(category)

    return tools


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
        使用 MiniMax API 的直接搜索端点
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

            # 使用 MiniMax 专门的搜索 API（固定URL）
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MINIMAX_API_KEY}"
            }
            
            payload = {"q": query}
            
            _log.info(f"[Search] 发送搜索请求: {query}")
            _log.info(f"[Search] API URL: {MINIMAX_SEARCH_URL}")
            
            # 发送请求
            import requests
            response = requests.post(MINIMAX_SEARCH_URL, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            
            # 解析响应
            result = response.json()
            
            _log.info(f"[Search] API响应: {json.dumps(result, ensure_ascii=False)[:500]}")
            
            # 解析搜索结果
            # MiniMax 搜索 API 返回格式可能是:
            # {"content": "搜索结果内容", ...} 或 {"results": [...], ...}
            formatted_results = []
            answer = ""
            
            # 方式1: 直接从 content 字段获取
            if "content" in result:
                answer = result.get("content", "")
                # 尝试提取 URL
                import re
                urls = re.findall(r'https?://[^\s\)\"\'\]]+', answer)
                for i, url in enumerate(urls[:num_results]):
                    formatted_results.append({
                        "title": f"搜索结果 {i+1}",
                        "snippet": answer[max(0, answer.find(url)-50):answer.find(url)+200] if url in answer else answer[:300],
                        "url": url
                    })
            
            # 方式2: 从 results 字段获取
            elif "results" in result:
                results_list = result.get("results", [])
                for i, item in enumerate(results_list[:num_results]):
                    formatted_results.append({
                        "title": item.get("title", f"结果 {i+1}"),
                        "snippet": item.get("snippet", item.get("content", ""))[:300],
                        "url": item.get("url", "")
                    })
                answer = "\n".join([f"{r['title']}: {r['url']}" for r in formatted_results])
            
            # 方式3: 原始返回
            else:
                answer = json.dumps(result, ensure_ascii=False, indent=2)
                formatted_results.append({
                    "title": "搜索结果",
                    "snippet": answer[:500],
                    "url": ""
                })
            
            return {
                "success": True,
                "query": query,
                "results": formatted_results,
                "answer": answer if answer else "未找到相关结果"
            }

        except requests.exceptions.RequestException as e:
            _log.error(f"[Search] 请求错误: {e}")
            return {
                "success": False,
                "error": f"搜索请求失败: {str(e)}",
                "query": query
            }
        except Exception as e:
            _log.error(f"[Search] 搜索错误: {e}")
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
    def understand_image(prompt: str, image_source: str) -> Dict[str, Any]:
        """
        图片理解（使用 MiniMax VLM API）
        
        Args:
            prompt: 询问图片的问题
            image_source: 图片URL或本地文件路径
        """
        try:
            # 检查配置
            if not MINIMAX_API_KEY:
                _log.error("MiniMax API密钥未配置")
                return {
                    "success": False,
                    "error": "MiniMax API密钥未配置"
                }
            
            # 处理图片源
            processed_image_url = process_image_url(image_source)
            
            # 构建请求
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MINIMAX_API_KEY}"
            }
            
            payload = {
                "prompt": prompt,
                "image_url": processed_image_url
            }
            
            _log.info(f"[VLM] 发送图片理解请求")
            _log.info(f"[VLM] Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}")
            _log.info(f"[VLM] Image: {image_source[:100]}{'...' if len(image_source) > 100 else ''}")
            
            # 发送请求
            import requests
            response = requests.post(MINIMAX_VLM_URL, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            
            # 解析响应
            result = response.json()
            
            _log.info(f"[VLM] API响应: {json.dumps(result, ensure_ascii=False)[:300]}")
            
            # 提取理解结果
            content = result.get("content", "")
            
            if not content:
                return {
                    "success": False,
                    "error": "图片理解返回结果为空"
                }
            
            return {
                "success": True,
                "content": content,
                "image_source": image_source,
                "prompt": prompt
            }
            
        except requests.exceptions.RequestException as e:
            _log.error(f"[VLM] 请求错误: {e}")
            return {
                "success": False,
                "error": f"图片理解请求失败: {str(e)}"
            }
        except Exception as e:
            _log.error(f"[VLM] 图片理解错误: {e}")
            return {
                "success": False,
                "error": str(e)
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
    },
    {
        "type": "function",
        "function": {
            "name": "understand_image",
            "description": "图片理解工具。当用户发送图片并询问相关内容时使用此工具，可以分析图片内容、识别物体、描述场景等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "询问图片的问题，如'这张图片里有什么'、'描述一下这个场景'等"
                    },
                    "image_source": {
                        "type": "string",
                        "description": "图片的URL地址或本地文件路径"
                    }
                },
                "required": ["prompt", "image_source"]
            }
        }
    }
]

# ========== 工作区工具定义 ==========
WORKSPACE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "workspace_create_file",
            "description": "在当前会话的工作区中创建或覆盖一个文件。适用于为用户生成代码、文档、配置文件等场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "文件名（支持子目录，如 'src/main.py'）"
                    },
                    "content": {
                        "type": "string",
                        "description": "文件的文本内容"
                    }
                },
                "required": ["filename", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_read_file",
            "description": "读取当前会话工作区中的文件内容。用于查看用户上传的文件或之前创建的文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要读取的文件名"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_edit_file",
            "description": "修改工作区中已有文件的部分内容（查找并替换）。适用于修改代码、更新配置等场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要修改的文件名"
                    },
                    "old_content": {
                        "type": "string",
                        "description": "要被替换的原始内容片段"
                    },
                    "new_content": {
                        "type": "string",
                        "description": "替换后的新内容"
                    }
                },
                "required": ["filename", "old_content", "new_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_delete_file",
            "description": "删除工作区中的指定文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要删除的文件名"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_list_files",
            "description": "列出当前会话工作区中的所有文件。用于查看工作区内有哪些文件。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_send_file",
            "description": "将工作区中的文件发送给用户。当用户需要下载或获取工作区中的文件时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要发送的文件名"
                    }
                },
                "required": ["filename"]
            }
        }
    }
]


def get_all_tool_definitions(include_workspace: bool = True) -> List[Dict]:
    """获取所有工具定义（包括工作区工具）"""
    tools = list(TOOL_DEFINITIONS)
    if include_workspace:
        tools.extend(WORKSPACE_TOOL_DEFINITIONS)
    return tools


def execute_tool(tool_name: str, arguments: Dict[str, Any], context: Dict = None) -> Dict[str, Any]:
    """
    执行指定的工具（优先使用 Web 配置）

    Args:
        tool_name: 工具名称
        arguments: 工具参数
        context: 可选的上下文信息，包含 session_id 等

    Returns:
        工具执行结果
    """
    # 0. 工作区工具 - 需要 context 中的 session_id
    if tool_name.startswith("workspace_"):
        return _execute_workspace_tool(tool_name, arguments, context)

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
        "understand_image": executor.understand_image,
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


def _execute_workspace_tool(tool_name: str, arguments: Dict[str, Any],
                            context: Dict = None) -> Dict[str, Any]:
    """执行工作区相关工具"""
    try:
        from nbot.core.workspace import workspace_manager
    except ImportError:
        return {"success": False, "error": "工作区模块不可用"}

    if not context or not context.get('session_id'):
        return {"success": False, "error": "缺少会话信息，无法操作工作区"}

    session_id = context['session_id']
    session_type = context.get('session_type', 'unknown')

    try:
        if tool_name == "workspace_create_file":
            return workspace_manager.create_file(
                session_id, arguments['filename'], arguments['content'], session_type)

        elif tool_name == "workspace_read_file":
            return workspace_manager.read_file(session_id, arguments['filename'])

        elif tool_name == "workspace_edit_file":
            return workspace_manager.edit_file(
                session_id, arguments['filename'],
                arguments['old_content'], arguments['new_content'])

        elif tool_name == "workspace_delete_file":
            return workspace_manager.delete_file(session_id, arguments['filename'])

        elif tool_name == "workspace_list_files":
            return workspace_manager.list_files(session_id)

        elif tool_name == "workspace_send_file":
            # 返回文件路径，由调用方负责实际发送
            file_path = workspace_manager.get_file_path(session_id, arguments['filename'])
            if file_path:
                return {
                    "success": True,
                    "action": "send_file",
                    "filename": arguments['filename'],
                    "path": file_path,
                    "size": os.path.getsize(file_path)
                }
            return {"success": False, "error": f"文件不存在: {arguments['filename']}"}

        else:
            return {"success": False, "error": f"未知的工作区工具: {tool_name}"}

    except Exception as e:
        _log.error(f"Workspace tool error: {tool_name} - {e}")
        return {"success": False, "error": str(e)}
