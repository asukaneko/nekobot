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

# 动态执行器（延迟导入避免循环依赖）
_dynamic_executor = None

def get_dynamic_executor():
    """获取动态执行器实例（延迟加载）"""
    global _dynamic_executor
    if _dynamic_executor is None:
        try:
            from nbot.services.dynamic_executor import get_executor
            _dynamic_executor = get_executor()
        except Exception as e:
            _log.error(f"Failed to load dynamic executor: {e}")
            _dynamic_executor = None
    return _dynamic_executor

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

# Exec 工具配置
EXEC_WHITELIST = {
    'ls', 'cat', 'echo', 'pwd', 'whoami', 'date', 'cal', 'df', 'du',
    'head', 'tail', 'wc', 'grep', 'find', 'ps', 'top', 'htop',
    'ping', 'curl', 'wget', 'git', 'python', 'python3', 'pip',
    'node', 'npm', 'yarn', 'docker', 'docker-compose', 'kubectl',
    'systemctl', 'service', 'journalctl', 'uname', 'hostname',
    'netstat', 'ss', 'lsof', 'ifconfig', 'ip', 'route',
    'tar', 'gzip', 'gunzip', 'zip', 'unzip', 'chmod', 'chown',
    'mkdir', 'touch', 'cp', 'mv', 'rm', 'rmdir', 'ln',
    'which', 'whereis', 'type', 'file', 'stat', 'md5sum', 'sha256sum',
    'cd', 'dir'
}

EXEC_BLACKLIST_PATTERNS = [
    r'rm\s+-rf\s+/',
    r'mkfs',
    r'dd\s+if=',
    r'>\s*/dev/',
    r':\(\)\s*\{',
    r'fork\s*\(',
    r'while\s*\(true\)',
    r'download.*exec',
    r'curl.*\|.*bash',
    r'wget.*\|.*sh',
]


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

    @staticmethod
    def exec_command(command: str, timeout: int = 30, confirmed: bool = False) -> Dict[str, Any]:
        """
        执行命令行命令
        
        Args:
            command: 要执行的命令
            timeout: 超时时间（秒），默认30秒
            confirmed: 是否已经用户确认，False表示需要检查确认
        
        Returns:
            命令执行结果，如果需要确认则返回确认请求
        """
        import subprocess
        import re
        import shlex
        
        try:
            # 安全检查：检测危险模式
            for pattern in EXEC_BLACKLIST_PATTERNS:
                if re.search(pattern, command, re.IGNORECASE):
                    return {
                        "success": False,
                        "error": f"命令包含危险操作模式，已阻止执行",
                        "command": command,
                        "blocked_reason": "dangerous_pattern"
                    }
            
            # 解析命令获取主命令名
            try:
                cmd_parts = shlex.split(command)
                main_cmd = cmd_parts[0] if cmd_parts else ""
            except:
                main_cmd = command.split()[0] if command else ""
            
            # 检查是否在白名单中
            is_whitelisted = main_cmd in EXEC_WHITELIST
            
            # 如果未确认且不在白名单，返回确认请求
            if not confirmed and not is_whitelisted:
                return {
                    "success": False,
                    "error": "需要用户确认",
                    "command": command,
                    "require_confirmation": True,
                    "main_command": main_cmd,
                    "is_whitelisted": is_whitelisted,
                    "message": f"AI 请求执行命令: `{command}`\n\n该命令不在白名单中，请谨慎确认。"
                }
            
            # 执行命令
            _log.info(f"Executing command: {command}")
            
            # 使用 subprocess 执行命令
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=os.getcwd()
            )
            
            # 构建返回结果
            output = result.stdout
            error_output = result.stderr
            
            # 限制输出长度
            max_output_length = 10000
            if len(output) > max_output_length:
                output = output[:max_output_length] + f"\n\n... (输出已截断，共 {len(result.stdout)} 字符)"
            
            return {
                "success": result.returncode == 0,
                "command": command,
                "returncode": result.returncode,
                "stdout": output,
                "stderr": error_output[:5000] if error_output else "",
                "is_whitelisted": is_whitelisted
            }
            
        except subprocess.TimeoutExpired:
            _log.error(f"Command timeout: {command}")
            return {
                "success": False,
                "error": f"命令执行超时（{timeout}秒）",
                "command": command
            }
        except Exception as e:
            _log.error(f"Exec command error: {e}")
            return {
                "success": False,
                "error": str(e),
                "command": command
            }

    @staticmethod
    def download_file(url: str, filename: str = None, workspace_id: str = None) -> Dict[str, Any]:
        """
        从 URL 下载文件到工作区
        
        Args:
            url: 文件下载链接
            filename: 保存的文件名（可选，默认从 URL 中提取）
            workspace_id: 工作区 ID（可选，默认使用当前会话的工作区）
        
        Returns:
            下载结果
        """
        try:
            # 如果没有指定文件名，从 URL 中提取
            if not filename:
                from urllib.parse import urlparse, unquote
                parsed = urlparse(url)
                filename = unquote(os.path.basename(parsed.path))
                if not filename:
                    filename = f"downloaded_file_{int(datetime.now().timestamp())}"
            
            # 确保文件名安全
            filename = os.path.basename(filename)  # 移除路径
            
            # 下载文件
            req = urllib.request.Request(
                url,
                headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }
            )
            
            _log.info(f"Downloading file from {url} to workspace {workspace_id}")
            
            with urllib.request.urlopen(req, timeout=60) as response:
                content = response.read()
                content_type = response.headers.get('Content-Type', 'application/octet-stream')
                
            # 确定文件类型
            file_type = 'file'
            if content_type.startswith('image/'):
                file_type = 'image'
            elif content_type.startswith('text/'):
                file_type = 'text'
            elif content_type == 'application/pdf':
                file_type = 'pdf'
            
            # 保存到工作区
            try:
                from nbot.core.workspace import workspace_manager
                
                # 如果没有指定 workspace_id，尝试使用默认工作区
                if not workspace_id:
                    # 尝试获取当前上下文中的工作区
                    workspace_id = 'default'
                
                # 创建工作区目录（如果不存在）
                workspace_path = workspace_manager.get_or_create(workspace_id)
                file_path = os.path.join(workspace_path, filename)
                
                # 保存文件
                with open(file_path, 'wb') as f:
                    f.write(content)
                
                _log.info(f"File saved to {file_path}")
                
                return {
                    "success": True,
                    "url": url,
                    "filename": filename,
                    "file_path": file_path,
                    "file_type": file_type,
                    "content_type": content_type,
                    "size": len(content),
                    "workspace_id": workspace_id,
                    "message": f"文件已成功下载到工作区: {filename}"
                }
                
            except Exception as e:
                _log.error(f"Failed to save file to workspace: {e}")
                return {
                    "success": False,
                    "error": f"保存文件到工作区失败: {str(e)}",
                    "url": url,
                    "filename": filename
                }
                
        except urllib.error.URLError as e:
            _log.error(f"Download URL error: {e}")
            return {
                "success": False,
                "error": f"下载失败: {str(e)}",
                "url": url
            }
        except Exception as e:
            _log.error(f"Download file error: {e}")
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
    },
    {
        "type": "function",
        "function": {
            "name": "save_to_memory",
            "description": "将重要信息保存到记忆管理系统。当用户要求记住某些信息、保存重要内容、记录关键事项时使用此工具。可以保存为长期记忆（永久保存）或短期记忆（自动过期）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "记忆的标题/关键词，用于标识这段记忆，如'用户的喜好'、'项目需求'、'重要日期'等"
                    },
                    "value": {
                        "type": "string",
                        "description": "要保存的记忆内容，详细描述需要记住的信息"
                    },
                    "mem_type": {
                        "type": "string",
                        "description": "记忆类型：'long'表示长期记忆（永久保存），'short'表示短期记忆（会在一定时间后自动过期），默认为'long'",
                        "enum": ["long", "short"],
                        "default": "long"
                    },
                    "expire_days": {
                        "type": "integer",
                        "description": "如果是短期记忆，设置过期天数（默认7天），长期记忆可忽略此参数",
                        "default": 7
                    }
                },
                "required": ["key", "value"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "读取已保存的记忆内容。当用户询问之前记住的内容、查询保存的信息、确认记忆中的内容时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "mem_type": {
                        "type": "string",
                        "description": "记忆类型筛选：'long'表示长期记忆，'short'表示短期记忆，不填则返回所有记忆",
                        "enum": ["long", "short"]
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "执行命令行命令。用于执行系统命令、运行脚本、查看系统状态等。白名单内的命令（如ls, cat, echo, git, python等）会直接执行，不在白名单中的命令需要用户确认。危险命令会被自动阻止。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令行命令，如'ls -la'、'cat file.txt'、'python script.py'等"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "命令超时时间（秒），默认30秒",
                        "default": 30
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "是否已经用户确认。首次调用时设为false，如果返回需要确认，则用户确认后再次调用设为true",
                        "default": False
                    }
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "download_file",
            "description": "从 URL 下载文件到工作区。当用户需要下载网络文件、保存图片、下载文档等时使用此工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "文件下载链接，如'https://example.com/file.pdf'"
                    },
                    "filename": {
                        "type": "string",
                        "description": "保存的文件名（可选，默认从 URL 中提取）"
                    },
                    "workspace_id": {
                        "type": "string",
                        "description": "工作区 ID（可选，默认使用当前会话的工作区）"
                    }
                },
                "required": ["url"]
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
            "description": "读取当前会话工作区中的文件内容。用于查看用户上传的文件或之前创建的文件。支持按行范围或字符范围读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要读取的文件名"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "开始行号（从1开始），与 end_line 配合使用可读取指定行范围。例如：start_line=10, end_line=20 表示读取第10到20行。"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号（包含），需要与 start_line 配合使用。例如：start_line=10, end_line=20 表示读取第10到20行。"
                    },
                    "char_count": {
                        "type": "integer",
                        "description": "读取的字符数量，从文件开头或 start_char 指定位置开始。与 start_char 配合可从任意位置读取指定长度。"
                    },
                    "start_char": {
                        "type": "integer",
                        "description": "从第几个字符开始读取（从0开始）。需要与 char_count 配合使用。例如：start_char=100, char_count=200 表示从第100个字符开始读取200个字符。"
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
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_parse_file",
            "description": "解析工作区中的文件内容。支持 PDF、DOCX、PPT、Excel、代码文件等。自动识别文件类型并提取文本内容。适用于需要理解文档内容的场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要解析的文件名"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最大提取字符数，默认为 50000。避免返回过长内容。",
                        "default": 50000
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_file_info",
            "description": "获取工作区中文件的元数据信息（不解析内容）。返回文件类型、大小、页数/工作表数等基本信息。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要查询的文件名"
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
        executor = get_dynamic_executor()
        if executor:
            return executor.execute_tool(tool_config, arguments, context)
        else:
            return {
                "success": False,
                "error": "Dynamic executor not available"
            }

    # 3. 处理记忆工具（需要 context 中的用户信息）
    if tool_name == "save_to_memory":
        return _execute_save_to_memory(arguments, context)
    
    if tool_name == "read_memory":
        return _execute_read_memory(arguments, context)

    # 4. 否则使用内置 Tool
    _log.info(f"Executing built-in tool: {tool_name}")
    executor = ToolExecutor()

    tool_map = {
        "search_news": executor.search_news,
        "get_weather": executor.get_weather,
        "search_web": executor.search_web,
        "get_date_time": executor.get_date_time,
        "http_get": executor.http_get,
        "understand_image": executor.understand_image,
        "exec_command": executor.exec_command,
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
        # 兼容 file_path 和 filename 两种参数名
        filename = arguments.get('filename') or arguments.get('file_path')
        
        if tool_name == "workspace_create_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            return workspace_manager.create_file(
                session_id, filename, arguments['content'], session_type)

        elif tool_name == "workspace_read_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            return workspace_manager.read_file(
                session_id, 
                filename,
                start_line=arguments.get('start_line'),
                end_line=arguments.get('end_line'),
                char_count=arguments.get('char_count'),
                start_char=arguments.get('start_char')
            )

        elif tool_name == "workspace_edit_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            return workspace_manager.edit_file(
                session_id, filename,
                arguments['old_content'], arguments['new_content'])

        elif tool_name == "workspace_delete_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            return workspace_manager.delete_file(session_id, filename)

        elif tool_name == "workspace_list_files":
            return workspace_manager.list_files(session_id)

        elif tool_name == "workspace_send_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            # 返回文件路径，由调用方负责实际发送
            file_path = workspace_manager.get_file_path(session_id, filename)
            if file_path:
                return {
                    "success": True,
                    "action": "send_file",
                    "filename": filename,
                    "path": file_path,
                    "size": os.path.getsize(file_path),
                    "message": f"文件 '{filename}' 已发送给用户，无需再次提及文件路径或内容。"
                }
            return {"success": False, "error": f"文件不存在: {arguments['filename']}"}

        elif tool_name == "workspace_parse_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename)"}
            file_path = workspace_manager.get_file_path(session_id, filename)
            if not file_path:
                return {"success": False, "error": f"文件不存在: {filename}"}
            
            # 使用文件解析器解析文件
            try:
                from nbot.core.file_parser import file_parser
                max_chars = arguments.get('max_chars', 50000)
                result = file_parser.parse_file(file_path, filename, max_chars)
                return result
            except Exception as e:
                _log.error(f"解析文件失败: {filename}, {e}")
                return {"success": False, "error": f"解析文件失败: {str(e)}"}

        elif tool_name == "workspace_file_info":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename)"}
            file_path = workspace_manager.get_file_path(session_id, filename)
            if not file_path:
                return {"success": False, "error": f"文件不存在: {filename}"}
            
            # 使用文件解析器获取元数据
            try:
                from nbot.core.file_parser import file_parser
                result = file_parser.get_file_metadata(file_path, filename)
                return result
            except Exception as e:
                _log.error(f"获取文件元数据失败: {filename}, {e}")
                return {"success": False, "error": f"获取文件元数据失败: {str(e)}"}

        else:
            return {"success": False, "error": f"未知的工作区工具: {tool_name}"}

    except Exception as e:
        _log.error(f"Workspace tool error: {tool_name} - {e}")
        return {"success": False, "error": str(e)}


def _execute_save_to_memory(arguments: Dict[str, Any], context: Dict = None) -> Dict[str, Any]:
    """执行保存到记忆工具"""
    try:
        from nbot.core.prompt import prompt_manager
        
        if not prompt_manager:
            return {"success": False, "error": "记忆管理系统不可用"}
        
        key = arguments.get('key', '')
        value = arguments.get('value', '')
        mem_type = arguments.get('mem_type', 'long')
        expire_days = arguments.get('expire_days', 7)
        
        if not key or not value:
            return {"success": False, "error": "缺少必需的参数: key 和 value"}
        
        # 从 context 获取目标ID（用户ID或群ID）
        target_id = ''
        if context:
            # 优先使用 user_id，然后是 group_id
            target_id = context.get('user_id', '') or context.get('group_id', '')
        
        # 添加记忆
        success = prompt_manager.add_memory(key, value, target_id, mem_type, expire_days)
        
        if success:
            mem_type_desc = "长期记忆" if mem_type == "long" else f"短期记忆（{expire_days}天）"
            return {
                "success": True,
                "message": f"已成功保存到{mem_type_desc}",
                "key": key,
                "type": mem_type
            }
        else:
            return {"success": False, "error": "保存记忆失败"}
            
    except Exception as e:
        _log.error(f"Save to memory error: {e}")
        return {"success": False, "error": f"保存记忆时出错: {str(e)}"}


def _execute_read_memory(arguments: Dict[str, Any], context: Dict = None) -> Dict[str, Any]:
    """执行读取记忆工具"""
    try:
        from nbot.core.prompt import prompt_manager
        
        if not prompt_manager:
            return {"success": False, "error": "记忆管理系统不可用"}
        
        # 可选参数
        mem_type = arguments.get('mem_type', None)  # 'long', 'short', 或 None（全部）
        
        # 从 context 获取目标ID（用户ID或群ID）
        target_id = None
        if context:
            target_id = context.get('user_id', '') or context.get('group_id', '')
            if not target_id:
                target_id = None
        
        # 获取记忆（按 target_id 过滤）
        memories = prompt_manager.get_memories(target_id, mem_type)
        
        if not memories:
            return {
                "success": True,
                "message": "没有找到任何记忆",
                "count": 0,
                "memories": []
            }
        
        # 格式化返回
        formatted_memories = []
        for mem in memories:
            mem_type_val = mem.get('type', 'long')
            mem_type_desc = "长期记忆" if mem_type_val == "long" else "短期记忆"
            created_at = mem.get('created_at', '未知时间')
            formatted_memories.append({
                "key": mem.get('key', ''),
                "value": mem.get('value', ''),
                "type": mem_type_desc,
                "created_at": created_at
            })
        
        return {
            "success": True,
            "message": f"共找到 {len(memories)} 条记忆",
            "count": len(memories),
            "memories": formatted_memories
        }
        
    except Exception as e:
        _log.error(f"Read memory error: {e}")
        return {"success": False, "error": f"读取记忆时出错: {str(e)}"}
