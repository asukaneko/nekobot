"""
工具模块 - 提供 AI 可调用的各种工具
"""
import json
import logging
import urllib.request
import urllib.parse
import os
import difflib
import mimetypes
import uuid
import time
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
def get_minimax_api_key() -> str:
    return (
        os.getenv("MINIMAX_API_KEY")
        or os.getenv("API_KEY")
        or config_parser.get("ApiKey", "api_key", fallback="")
    )

# MiniMax API 配置（仅用于 understand_image 工具）
MINIMAX_API_KEY = config_parser.get('ApiKey', 'api_key', fallback="")

# 固定工具 API URL
MINIMAX_VLM_URL = "https://api.minimaxi.com/v1/coding_plan/vlm"

# Web 配置数据目录
WEB_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'web')

# Exec 工具配置
EXEC_WHITELIST = {
    'ls', 'cat', 'echo', 'pwd', 'whoami', 'date', 'cal', 'df', 'du',
    'head', 'tail', 'wc', 'grep', 'find', 'ps', 'top', 'htop',
    'ping', 'journalctl', 'uname', 'hostname',
    'netstat', 'ss', 'lsof', 'ifconfig', 'ip', 'route',
    'which', 'whereis', 'type', 'file', 'stat', 'md5sum', 'sha256sum',
}

EXEC_BLACKLIST_PATTERNS = [
    r'rm\s+-rf\s+/',
    r'mkfs',
    r'dd\s+if=',
    r'>\s*/dev/',
    r':\(\)\s*\{',
    r'fork\s*\(',
    r'while\s*\(true\)',
]

# 待执行命令存储（用于确认机制）
# request_id -> {command, timeout, session_id, timestamp}
_pending_executions: Dict[str, Dict] = {}
# session_id -> request_id（用于 QQ 通道通过 session 查找）
_session_pending: Dict[str, str] = {}

# 确认关键词（QQ 通道用于检测用户是否同意执行）
_CONFIRM_KEYWORDS = {'确认', '同意', '确认执行', '是', 'yes', 'y', 'ok', '执行'}
_REJECT_KEYWORDS = {'取消', '拒绝', '否', '不执行', 'no', 'n', 'cancel'}

def store_pending_execution(session_id: str, command: str, timeout: int = 30) -> str:
    """存储待确认的命令，返回 request_id"""
    request_id = uuid.uuid4().hex
    _pending_executions[request_id] = {
        'command': command,
        'timeout': timeout,
        'session_id': session_id,
        'timestamp': time.time(),
    }
    _session_pending[session_id] = request_id
    _log.info(f"[PendingExec] 存储待确认命令: request_id={request_id[:8]}, session={session_id}, cmd={command[:80]}")
    return request_id

def get_pending_by_session(session_id: str) -> Optional[str]:
    """通过 session_id 查找待确认命令的 request_id"""
    return _session_pending.get(session_id)

def get_pending_info(request_id: str) -> Optional[Dict]:
    """查看待执行命令信息"""
    return _pending_executions.get(request_id)

def execute_pending_command(request_id: str) -> Dict[str, Any]:
    """执行待确认的命令，返回执行结果"""
    import subprocess
    import shlex

    pending = _pending_executions.pop(request_id, None)
    if not pending:
        return {"success": False, "error": "未找到待执行的命令，可能已过期或已处理", "request_id": request_id}

    # 清理 session 映射
    session_id = pending.get('session_id', '')
    if _session_pending.get(session_id) == request_id:
        del _session_pending[session_id]

    command = pending['command']
    timeout = pending.get('timeout', 30)

    _log.info(f"[PendingExec] 用户确认，执行命令: {command}")

    try:
        try:
            cmd_parts = shlex.split(command)
        except Exception:
            cmd_parts = command.split()

        result = subprocess.run(
            cmd_parts,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=os.getcwd()
        )

        output = result.stdout
        max_output_length = 10000
        if len(output) > max_output_length:
            output = output[:max_output_length] + f"\n\n... (输出已截断，共 {len(result.stdout)} 字符)"

        return {
            "success": result.returncode == 0,
            "command": command,
            "returncode": result.returncode,
            "stdout": output,
            "stderr": result.stderr[:5000] if result.stderr else "",
            "executed": True,
        }
    except subprocess.TimeoutExpired:
        _log.error(f"[PendingExec] 命令超时: {command}")
        return {
            "success": False,
            "error": f"命令执行超时（{timeout}秒）",
            "command": command,
        }
    except Exception as e:
        _log.error(f"[PendingExec] 执行出错: {e}")
        return {
            "success": False,
            "error": str(e),
            "command": command,
        }

def reject_pending_command(request_id: str) -> Dict[str, Any]:
    """拒绝待确认的命令，清理存储"""
    pending = _pending_executions.pop(request_id, None)
    session_id = pending.get('session_id', '') if pending else ''
    if session_id and _session_pending.get(session_id) == request_id:
        del _session_pending[session_id]

    if pending:
        _log.info(f"[PendingExec] 用户拒绝命令: request_id={request_id[:8]}, cmd={pending.get('command', '')[:80]}")
        return {"success": False, "rejected": True, "command": pending.get('command', ''), "message": "用户已拒绝执行该命令"}
    else:
        return {"success": False, "rejected": True, "error": "未找到待执行的命令", "request_id": request_id}


def _truncate_text_preview(text: str, limit: int = 1200) -> str:
    text = (text or "").replace("\r\n", "\n")
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>"


def _read_text_preview(path: str, limit: int = 1200) -> Optional[str]:
    if not path or not os.path.exists(path) or os.path.isdir(path):
        return None
    mime_type, _ = mimetypes.guess_type(path)
    if mime_type and not (
        mime_type.startswith("text/")
        or mime_type
        in {
            "application/json",
            "application/xml",
            "application/yaml",
            "application/javascript",
            "application/x-python",
        }
    ):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return _truncate_text_preview(f.read(), limit=limit)
    except Exception:
        return None


def _build_diff_preview(before_text: str, after_text: str, max_lines: int = 80) -> str:
    before_lines = (before_text or "").replace("\r\n", "\n").splitlines()
    after_lines = (after_text or "").replace("\r\n", "\n").splitlines()
    diff_lines = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile="before",
            tofile="after",
            lineterm="",
        )
    )
    if len(diff_lines) > max_lines:
        diff_lines = diff_lines[:max_lines] + ["...<diff truncated>"]
    return "\n".join(diff_lines)


def _build_workspace_change(
    *,
    action: str,
    filename: str,
    scope: str,
    before_text: Optional[str] = None,
    after_text: Optional[str] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    max_preview_chars = 4000
    preview_too_large = (
        len(before_text or "") > max_preview_chars
        or len(after_text or "") > max_preview_chars
    )
    before_preview = (
        _truncate_text_preview(before_text or "") if before_text is not None and not preview_too_large else None
    )
    after_preview = (
        _truncate_text_preview(after_text or "") if after_text is not None and not preview_too_large else None
    )
    change = {
        "action": action,
        "path": filename,
        "scope": scope,
        "preview_too_large": preview_too_large,
    }
    if path:
        change["absolute_path"] = path
    if before_preview is not None:
        change["before_preview"] = before_preview
    if after_preview is not None:
        change["after_preview"] = after_preview
    if (before_text is not None or after_text is not None) and not preview_too_large:
        change["diff_preview"] = _build_diff_preview(before_text or "", after_text or "")
    return change


def _append_workspace_change(result: Dict[str, Any], change: Dict[str, Any]) -> Dict[str, Any]:
    file_changes = list(result.get("file_changes") or [])
    file_changes.append(change)
    result["file_changes"] = file_changes
    result["change_summary"] = {
        "created": sum(1 for item in file_changes if item.get("action") == "created"),
        "modified": sum(1 for item in file_changes if item.get("action") == "modified"),
        "deleted": sum(1 for item in file_changes if item.get("action") == "deleted"),
    }
    return result


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

    自动从注册表获取所有已注册的工具
    """
    # 从注册表获取所有装饰器注册的工具
    from nbot.services.tool_registry import get_all_tool_definitions as get_registry_tools
    registered_tools = get_registry_tools()

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

    # 添加 Todo 工具
    from nbot.services.todo_tools import TODO_TOOL_DEFINITIONS
    tools.extend(TODO_TOOL_DEFINITIONS)

    # 添加注册表中的工具
    tools.extend(registered_tools)

    return tools


class ToolExecutor:
    """工具执行器"""

    @staticmethod
    def search_news(query: str = "热点新闻", count: int = 5, source: str = "all") -> Dict[str, Any]:
        """
        搜索新闻
        使用 RSS 源获取新闻，并根据关键词过滤

        Args:
            query: 搜索关键词（用于过滤新闻标题和摘要）
            count: 返回的新闻数量（默认5条）
            source: 新闻来源，可选值：
                - "all": 所有可用源（默认）
                - "36kr": 36氪（科技、创业、投资）
                - "ithome": IT之家（科技数码）
                - "huxiu": 虎嗅（商业、科技）
                - "sspai": 少数派（数码、效率工具）

        注意：由于网络限制，部分 RSS 源可能无法访问
        """
        try:
            import feedparser
            import socket
            import re

            # 设置超时
            socket.setdefaulttimeout(5)

            # 所有可用的 RSS 新闻源
            all_sources = {
                "36kr": ("36氪", "https://36kr.com/feed"),
                "ithome": ("IT之家", "https://www.ithome.com/rss/"),
                "huxiu": ("虎嗅", "https://www.huxiu.com/rss/0.xml"),
                "sspai": ("少数派", "https://sspai.com/feed"),
            }

            # 根据参数选择新闻源
            if source.lower() == "all":
                rss_urls = list(all_sources.values())
            elif source.lower() in all_sources:
                rss_urls = [all_sources[source.lower()]]
            else:
                # 尝试模糊匹配
                matched = None
                for key in all_sources:
                    if source.lower() in key or key in source.lower():
                        matched = key
                        break
                if matched:
                    rss_urls = [all_sources[matched]]
                else:
                    return {
                        "success": False,
                        "query": query,
                        "error": f"未知的新闻源: {source}\n可用源: all, 36kr, ithome, huxiu, sspai"
                    }

            # 获取更多新闻用于过滤（每个源获取 count * 3 条）
            fetch_count = max(count * 3, 15)
            all_news = []
            failed_sources = []

            for source_name, url in rss_urls:
                try:
                    _log.info(f"[SearchNews] 尝试 RSS 源: {source_name}")
                    feed = feedparser.parse(url)

                    if feed.entries and len(feed.entries) > 0:
                        _log.info(f"[SearchNews] {source_name} 获取成功，共 {len(feed.entries)} 条")
                        for entry in feed.entries[:fetch_count]:
                            # 清理 HTML 标签
                            title = entry.get("title", "").replace("<![CDATA[", "").replace("]]>", "").strip()
                            summary = entry.get("summary", entry.get("description", ""))
                            # 简单清理 HTML
                            summary = re.sub(r'<[^>]+>', '', summary)
                            summary = summary.replace("<![CDATA[", "").replace("]]>", "").strip()[:300]

                            all_news.append({
                                "title": title,
                                "summary": summary,
                                "link": entry.get("link", ""),
                                "published": entry.get("published", datetime.now().isoformat()),
                                "source": source_name
                            })
                    else:
                        failed_sources.append(source_name)
                        _log.warning(f"[SearchNews] {source_name} 返回空数据")

                except Exception as e:
                    failed_sources.append(source_name)
                    _log.warning(f"[SearchNews] RSS 源 {source_name} 失败: {type(e).__name__}: {e}")
                    continue

            if not all_news:
                # 如果所有 RSS 都失败，返回友好的错误提示
                error_msg = (
                    f"🔍 新闻搜索暂时不可用\n\n"
                    f"尝试的 RSS 源: {', '.join(failed_sources) if failed_sources else '无'}\n\n"
                    f"可能的原因:\n"
                    f"• 网络连接问题\n"
                    f"• RSS 源被限制或不可访问\n"
                    f"• 需要配置网络代理\n\n"
                    f"💡 建议:\n"
                    f"1. 检查网络连接是否正常\n"
                    f"2. 如果使用代理，确保代理配置正确\n"
                    f"3. 或直接让 AI 模型使用其内置的联网搜索功能"
                )
                return {
                    "success": False,
                    "query": query,
                    "error": error_msg,
                    "hint": "部分新闻源可能需要网络代理才能访问"
                }

            # 根据关键词过滤新闻
            query_lower = query.lower()
            # 分词处理（支持中文和英文）
            keywords = re.split(r'[\s,，、]+', query_lower)
            keywords = [k for k in keywords if len(k) > 0]

            matched_news = []
            other_news = []

            for news in all_news:
                title_lower = news["title"].lower()
                summary_lower = news["summary"].lower()
                
                # 计算匹配分数
                score = 0
                for keyword in keywords:
                    if keyword in title_lower:
                        score += 10  # 标题匹配权重更高
                    if keyword in summary_lower:
                        score += 3   # 摘要匹配
                
                if score > 0:
                    news["match_score"] = score
                    matched_news.append(news)
                else:
                    other_news.append(news)

            # 按匹配分数排序
            matched_news.sort(key=lambda x: x.get("match_score", 0), reverse=True)

            # 组合结果：优先返回匹配的新闻，不足则补充其他新闻
            news_list = matched_news[:count]
            
            if len(news_list) < count:
                # 补充其他新闻
                remaining = count - len(news_list)
                news_list.extend(other_news[:remaining])

            # 移除匹配分数字段（不需要返回给用户）
            for news in news_list:
                news.pop("match_score", None)

            # 构建返回结果
            result = {
                "success": True,
                "query": query,
                "source": source,
                "total_fetched": len(all_news),
                "matched_count": len(matched_news),
                "news": news_list[:count]
            }

            # 如果没有匹配的新闻，添加提示
            if len(matched_news) == 0 and news_list:
                result["hint"] = f"未找到与 '{query}' 直接相关的新闻，返回最新新闻"

            return result

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
        使用搜狗搜索（无需 API key），并抓取页面正文获取详细内容
        """
        try:
            import requests
            from bs4 import BeautifulSoup
            from concurrent.futures import ThreadPoolExecutor, as_completed

            _log.info(f"[Search] 发送搜索请求: {query}")

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }

            # 搜狗搜索
            search_url = "https://www.sogou.com/web"
            search_params = {"query": query, "num": str(num_results * 2)}
            response = requests.get(search_url, headers=headers, params=search_params, timeout=15)
            response.encoding = 'utf-8'

            soup = BeautifulSoup(response.text, 'html.parser')
            vrwraps = soup.find_all('div', class_='vrwrap')

            formatted_results = []
            for i, vr in enumerate(vrwraps):
                if len(formatted_results) >= num_results:
                    break
                try:
                    h3 = vr.find('h3')
                    if not h3:
                        continue
                    link_tag = h3.find('a')
                    if not link_tag:
                        continue
                    title = link_tag.get_text(strip=True)
                    href = link_tag.get('href', '')

                    # 处理搜狗链接：相对路径补全，已有完整 URL 的直接用
                    if href.startswith('/link'):
                        href = 'https://www.sogou.com' + href

                    # 摘要：找容器内最长的有意义文本
                    snippet = ""
                    for tag in vr.find_all(['p', 'div', 'span']):
                        text = tag.get_text(strip=True)
                        if len(text) > len(snippet) and len(text) > 30 and text != title:
                            snippet = text

                    formatted_results.append({
                        "title": title,
                        "snippet": snippet[:500],
                        "url": href,
                        "content": ""
                    })
                except Exception as e:
                    _log.warning(f"[Search] 解析结果 {i} 失败: {e}")
                    continue

            if not formatted_results:
                return {
                    "success": False,
                    "error": "未找到搜索结果",
                    "query": query
                }

            # 并发抓取每个页面的正文内容
            def resolve_sogou_url(url: str) -> str:
                """解析搜狗跳转链接，提取真实 URL"""
                if 'sogou.com/link' not in url:
                    return url
                try:
                    import re as _re
                    resp = requests.get(url, headers=headers, timeout=5)
                    match = _re.search(r'window\.location\.replace\("(.*?)"\)', resp.text)
                    if match:
                        return match.group(1)
                    match = _re.search(r"content=[\"']0;URL='(.*?)'", resp.text)
                    if match:
                        return match.group(1)
                except Exception:
                    pass
                return url

            def fetch_page_content(url: str) -> tuple:
                """抓取页面并提取正文文本，返回 (真实URL, 正文内容)"""
                try:
                    real_url = resolve_sogou_url(url)
                    resp = requests.get(real_url, headers=headers, timeout=8, allow_redirects=True)
                    resp.encoding = resp.apparent_encoding or 'utf-8'
                    page_soup = BeautifulSoup(resp.text, 'html.parser')

                    for tag in page_soup.find_all(['script', 'style', 'nav', 'header', 'footer', 'aside', 'iframe', 'noscript']):
                        tag.decompose()

                    main_content = page_soup.find('article') or page_soup.find('main') or page_soup.find('div', class_=lambda c: c and ('content' in c or 'article' in c or 'post' in c))

                    if main_content:
                        paragraphs = main_content.find_all(['p', 'h1', 'h2', 'h3', 'li'])
                    else:
                        paragraphs = page_soup.find_all('p')

                    texts = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 15]
                    return (real_url, '\n'.join(texts)[:1500])
                except Exception as e:
                    _log.warning(f"[Search] 抓取页面失败 {url}: {e}")
                    return (url, "")

            with ThreadPoolExecutor(max_workers=num_results) as executor:
                future_map = {
                    executor.submit(fetch_page_content, r['url']): i
                    for i, r in enumerate(formatted_results) if r['url']
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        real_url, content = future.result()
                        formatted_results[idx]['url'] = real_url
                        formatted_results[idx]['content'] = content
                    except Exception:
                        pass

            # 生成易读的答案摘要
            answer = f"搜索「{query}」找到 {len(formatted_results)} 条结果：\n\n"
            for i, r in enumerate(formatted_results, 1):
                answer += f"【{i}】{r['title']}\n"
                answer += f"链接: {r['url']}\n"
                if r['content']:
                    answer += f"正文摘要:\n{r['content'][:600]}\n"
                else:
                    answer += f"摘要: {r['snippet']}\n"
                answer += "\n"

            return {
                "success": True,
                "query": query,
                "results": formatted_results,
                "answer": answer.strip()
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
            api_key = get_minimax_api_key()
            if not api_key:
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
                "Authorization": f"Bearer {api_key}"
            }
            
            payload = {
                "prompt": prompt,
                "image_url": processed_image_url
            }
            
            _log.info("[VLM] 发送图片理解请求")
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
    def exec_command(command: str, timeout: int = 30) -> Dict[str, Any]:
        """
        执行命令行命令

        Args:
            command: 要执行的命令
            timeout: 超时时间（秒），默认30秒

        Returns:
            命令执行结果，如果在白名单外则返回确认请求
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
                        "error": "命令包含危险操作模式，已阻止执行",
                        "command": command,
                        "blocked_reason": "dangerous_pattern"
                    }

            # 解析命令获取主命令名
            try:
                cmd_parts = shlex.split(command)
                main_cmd = cmd_parts[0] if cmd_parts else ""
            except:
                main_cmd = command.split()[0] if command else ""
                cmd_parts = command.split() if command else []

            if not cmd_parts:
                return {
                    "success": False,
                    "error": "命令不能为空",
                    "command": command,
                }

            # 检查是否在白名单中
            is_whitelisted = main_cmd in EXEC_WHITELIST

            # 不在白名单：返回确认请求（由 execute_tool 负责存储待执行状态）
            if not is_whitelisted:
                return {
                    "success": False,
                    "error": "需要用户确认",
                    "command": command,
                    "require_confirmation": True,
                    "main_command": main_cmd,
                    "is_whitelisted": False,
                    "message": f"AI 请求执行命令: `{command}`\n\n该命令不在白名单中，需用户确认后执行。"
                }

            # 白名单命令：直接执行
            _log.info(f"Executing command (whitelisted): {command}")

            result = subprocess.run(
                cmd_parts,
                shell=False,
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
                "is_whitelisted": True
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

    @staticmethod
    def send_message(content: str, message_type: str = "info", session_id: str = None) -> Dict[str, Any]:
        """
        向用户发送消息（不中断思考流程）
        
        Args:
            content: 消息内容
            message_type: 消息类型，可选 info/progress/warning/success
            session_id: 会话 ID（从 context 自动获取）
        
        Returns:
            发送结果
        """
        try:
            _log.info(f"[SendMessage] AI 发送{type}消息: {content[:50]}...")
            
            # 返回特殊标记，让调用者知道需要发送消息
            return {
                "success": True,
                "action": "send_message",
                "content": content,
                "message_type": message_type,
                "session_id": session_id,
                "message": "消息已发送给用户"
            }
            
        except Exception as e:
            _log.error(f"[SendMessage] 发送消息失败: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def get_session_thinking_history(limit: int = 10, session_id: str = None) -> Dict[str, Any]:
        """
        查询当前会话的历史思考记录（thinking_cards）

        Args:
            limit: 返回的历史记录数量限制
            session_id: 会话 ID（从 context 自动获取）

        Returns:
            历史思考记录列表
        """
        try:
            import os
            import json

            if not session_id:
                return {
                    "success": False,
                    "error": "未提供 session_id"
                }

            _log.info(f"[ThinkingHistory] 查询会话 {session_id[:8]}... 的历史思考记录，limit={limit}")

            # 尝试从 WebServer 实例获取会话数据
            sessions = None
            try:
                from nbot.web.server import WebServer
                web_server = WebServer.get_instance()
                if web_server:
                    sessions = web_server.sessions
            except:
                pass

            # 如果没有实例化的 WebServer，从文件读取
            if not sessions:
                sessions_file = 'data/web/sessions.json'
                if os.path.exists(sessions_file):
                    with open(sessions_file, 'r', encoding='utf-8') as f:
                        sessions_data = json.load(f)
                        sessions = sessions_data

            if not sessions:
                return {
                    "success": False,
                    "error": "无法获取会话数据"
                }

            # 获取会话
            session = sessions.get(session_id)
            if not session:
                # 尝试在 sessions_data 中查找
                if isinstance(sessions, dict):
                    for sid, sess in sessions.items():
                        if sid == session_id or (isinstance(sess, dict) and sess.get('id') == session_id):
                            session = sess
                            break

            if not session:
                return {
                    "success": False,
                    "error": f"未找到会话: {session_id}"
                }

            # 获取消息
            messages = session.get('messages', [])
            if not messages:
                return {
                    "success": True,
                    "history": [],
                    "count": 0,
                    "message": "该会话没有消息历史"
                }

            # 收集 thinking_cards
            thinking_records = []
            for msg in messages:
                thinking_cards = msg.get('thinking_cards', [])
                if thinking_cards:
                    for card in thinking_cards:
                        record = {
                            "timestamp": card.get('timestamp', ''),
                            "role": card.get('role', ''),
                            "steps": []
                        }

                        # 处理每个步骤
                        for step in card.get('steps', []):
                            step_info = {
                                "type": step.get('type', ''),
                                "name": step.get('name', ''),
                                "status": step.get('status', ''),
                            }

                            # 添加详细信息（用于工具调用）
                            if step.get('detail'):
                                step_info['detail'] = str(step['detail'])[:200]  # 限制长度

                            # 添加完整参数
                            if step.get('arguments'):
                                step_info['arguments'] = step['arguments']

                            # 添加完整结果
                            if step.get('full_result'):
                                full_result = step['full_result']
                                if isinstance(full_result, dict):
                                    # 提取关键信息
                                    if full_result.get('success') is not None:
                                        step_info['result'] = full_result.get('success')
                                    if full_result.get('content'):
                                        content = full_result['content']
                                        if isinstance(content, str):
                                            step_info['content_preview'] = content[:300]
                                        elif isinstance(content, list):
                                            step_info['items_count'] = len(content)
                                    if full_result.get('files'):
                                        step_info['files'] = full_result['files']
                                    if full_result.get('query'):
                                        step_info['query'] = full_result['query']
                                    if full_result.get('results'):
                                        results = full_result['results']
                                        if isinstance(results, list) and len(results) > 0:
                                            step_info['results_count'] = len(results)
                                            step_info['results_preview'] = str(results)[:500]
                                else:
                                    step_info['full_result'] = str(full_result)[:500]

                            record['steps'].append(step_info)

                        if record['steps']:
                            thinking_records.append(record)

            # 限制返回数量（按时间倒序）
            thinking_records = thinking_records[-limit:] if limit > 0 else thinking_records

            _log.info(f"[ThinkingHistory] 返回 {len(thinking_records)} 条历史记录")

            return {
                "success": True,
                "history": thinking_records,
                "count": len(thinking_records),
                "message": f"成功获取 {len(thinking_records)} 条历史思考记录"
            }

        except Exception as e:
            _log.error(f"[ThinkingHistory] 查询历史失败: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }


# 工具定义（用于 AI 工具调用）
TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_news",
            "description": "搜索最新新闻。当用户需要获取新闻资讯时使用此工具。可以指定新闻来源。",
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
                    },
                    "source": {
                        "type": "string",
                        "description": "新闻来源，可选值：'all'(所有源，默认)、'36kr'(36氪-科技创业)、'ithome'(IT之家-科技数码)、'huxiu'(虎嗅-商业科技)、'sspai'(少数派-数码效率)",
                        "enum": ["all", "36kr", "ithome", "huxiu", "sspai"],
                        "default": "all"
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
            "description": "将重要信息保存到记忆管理系统。当用户要求记住某些信息、保存重要内容、记录关键事项时使用此工具。可以保存为长期记忆（永久保存）或短期记忆（自动过期）。记忆包含标题（简短概括）、摘要（内容要点）和完整内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "记忆的标题，简短概括记忆的主题，如'用户喜好'、'项目需求'、'重要日期'等"
                    },
                    "content": {
                        "type": "string",
                        "description": "要保存的完整记忆内容，详细描述需要记住的信息"
                    },
                    "summary": {
                        "type": "string",
                        "description": "内容摘要，简短描述内容的要点，方便快速回顾（如果不提供，系统会自动从content提取前100字作为摘要）"
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
                "required": ["title", "content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_memory",
            "description": "读取已保存的记忆内容。当用户询问之前记住的内容、查询保存的信息、确认记忆中的内容时使用此工具。返回的记忆包含标题、摘要和完整内容。",
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
            "description": "执行命令行命令。白名单命令（如 ls, cat, echo 等）直接执行，非白名单命令系统会自动请求用户确认后执行。可通过 Web 管理界面启用或禁用此工具。",
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
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": "在思考过程中向用户发送消息，不中断思考流程。当AI需要长时间处理时，可以使用此工具告知用户当前进度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "要发送给用户的消息内容"
                    },
                    "message_type": {
                        "type": "string",
                        "description": "消息类型：info(普通信息)/progress(进度通知)/warning(警告)/success(成功通知)",
                        "enum": ["info", "progress", "warning", "success"],
                        "default": "info"
                    }
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_session_thinking_history",
            "description": "查询当前会话的历史思考记录（thinking_cards）。当需要了解之前使用了哪些工具、获得了什么结果时使用此工具。特别适用于长时间对话中，AI需要回顾之前操作的情况。此工具返回历史消息中的工具调用记录，包括工具名称、参数和完整结果。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "返回的历史记录数量限制，默认10条",
                        "default": 10
                    }
                }
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
            "description": "在工作区中创建或覆盖一个文件。适用于为用户生成代码、文档、配置文件等场景。",
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
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
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
            "description": "读取工作区中的文件内容。用于查看用户上传的文件或之前创建的文件。支持按行范围或字符范围读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "要读取的文件名"
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
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
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
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
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
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
            "description": "列出工作区中的所有文件。支持列出私有工作区和共享工作区的文件。支持递归列出子目录中的所有文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared", "all"],
                        "description": "工作区类型：'private' 仅列出当前会话私有工作区，'shared' 仅列出共享工作区，'all' 同时列出两者（默认）。"
                    },
                    "path": {
                        "type": "string",
                        "description": "要列出的子目录路径（可选）。例如：'docs' 或 'docs/src'。"
                    },
                    "recursive": {
                        "type": "boolean",
                        "description": "是否递归列出所有子目录中的文件。默认为 false。设置为 true 时，会列出 path 下所有文件夹中的文件，并标注完整路径。"
                    }
                }
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
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
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
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
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
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
                    }
                },
                "required": ["filename"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_skill_copy",
            "description": "将指定的 Skill 或 Skill 下的文件复制到工作区。适用于需要将 Skill 代码保存到工作区进行编辑或构建的场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "要复制的 Skill ID（如 'search'、'image_search'）"
                    },
                    "filename": {
                        "type": "string",
                        "description": "可选，要复制的 Skill 下的具体文件名。如不指定则复制整个 Skill。"
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["private", "shared"],
                        "description": "复制到的工作区类型：'private' 表示当前会话私有工作区，'shared' 表示所有会话共享工作区。默认 'private'。"
                    }
                },
                "required": ["skill_id"]
            }
        }
    }
]


def get_all_tool_definitions(include_workspace: bool = True) -> List[Dict]:
    """获取所有工具定义（包括工作区工具和注册的工具）"""
    tools = list(TOOL_DEFINITIONS)

    # 添加 Todo 工具定义
    from nbot.services.todo_tools import TODO_TOOL_DEFINITIONS
    tools.extend(TODO_TOOL_DEFINITIONS)

    # 导入 Skills 工具以触发装饰器注册
    try:
        from nbot.services import skills_tools
        # 从注册表获取所有装饰器注册的工具
        from nbot.services.tool_registry import get_all_tool_definitions as get_registered_tools
        registered_tools = get_registered_tools()
        _log.info(f"[Tools] 从注册表加载了 {len(registered_tools)} 个工具: {[t.get('function', {}).get('name') for t in registered_tools]}")
        tools.extend(registered_tools)
    except ImportError as e:
        _log.warning(f"[Tools] 注册表工具加载失败: {e}")
    except Exception as e:
        _log.warning(f"[Tools] 注册表工具处理失败: {e}")

    if include_workspace:
        tools.extend(WORKSPACE_TOOL_DEFINITIONS)
    return tools


def execute_tool(tool_name: str, arguments: Dict[str, Any], context: Dict = None) -> Dict[str, Any]:
    """
    执行指定的工具（优先使用注册表，然后是 Web 配置）

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

    # 1. 优先从注册表获取（使用装饰器注册的工具）
    from nbot.services.tool_registry import get_registry
    registry = get_registry()
    executor = registry.get_executor(tool_name)
    if executor:
        try:
            return executor(arguments, context)
        except Exception as e:
            _log.error(f"Tool execution error: {tool_name} - {e}")
            return {"success": False, "error": str(e)}

    # 2. 处理 Todo 工具（优先于 Web 配置检查）
    if tool_name.startswith("todo_"):
        from nbot.services.todo_tools import execute_todo_tool
        return execute_todo_tool(tool_name, arguments, context)

    # 3. 处理记忆工具（需要 context 中的用户信息）
    if tool_name == "save_to_memory":
        return _execute_save_to_memory(arguments, context)
    
    if tool_name == "read_memory":
        return _execute_read_memory(arguments, context)

    # 3. 从 Web 配置查找
    web_config = load_tools_config()
    tool_config = None

    for config in web_config:
        if config.get('name') == tool_name:
            tool_config = config
            break

    # 4. 如果找到 Web 配置且有 implementation，使用动态执行
    result = None
    if tool_config and tool_config.get('implementation'):
        _log.info(f"Executing dynamic tool: {tool_name}")
        executor = get_dynamic_executor()
        if executor:
            result = executor.execute_tool(tool_config, arguments, context)
        else:
            result = {
                "success": False,
                "error": "Dynamic executor not available"
            }
    else:
        # 5. 否则使用内置 Tool
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
            "get_session_thinking_history": executor.get_session_thinking_history,
        }

        if tool_name not in tool_map:
            return {
                "success": False,
                "error": f"Unknown tool: {tool_name}"
            }

        try:
            tool_func = tool_map[tool_name]
            result = tool_func(**arguments)
        except Exception as e:
            _log.error(f"Tool execution error: {e}")
            return {
                "success": False,
                "error": str(e)
            }

    # 如果 exec_command 返回需要确认，存储待执行命令并注入 request_id
    if result and result.get('require_confirmation') and context and context.get('session_id'):
        request_id = store_pending_execution(
            context['session_id'],
            result.get('command', ''),
            arguments.get('timeout', 30)
        )
        result['request_id'] = request_id
    return result


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
        
        # 获取 scope 参数，默认 'private'
        scope = arguments.get('scope', 'private')
        is_shared = (scope == 'shared')
        if is_shared:
            target_path = os.path.normpath(
                os.path.join(workspace_manager.get_shared_workspace(), filename or "")
            )
        else:
            workspace_root = workspace_manager.get_or_create(session_id, session_type)
            target_path = os.path.normpath(os.path.join(workspace_root, filename or ""))
        target_exists_before = os.path.exists(target_path) if filename else False
        target_preview_before = _read_text_preview(target_path) if target_exists_before else None
        
        if tool_name == "workspace_create_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            if 'content' not in arguments:
                return {"success": False, "error": "缺少 content 参数"}
            
            if is_shared:
                result = workspace_manager.create_shared_file(filename, arguments['content'])
            else:
                result = workspace_manager.create_file(
                    session_id, filename, arguments['content'], session_type)
            result['scope'] = scope
            if result.get('success'):
                _append_workspace_change(
                    result,
                    _build_workspace_change(
                        action='modified' if target_exists_before else 'created',
                        filename=result.get('filename', filename),
                        scope=scope,
                        before_text=target_preview_before,
                        after_text=arguments.get('content', ''),
                        path=result.get('path', target_path),
                    ),
                )
            return result

        elif tool_name == "workspace_read_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            
            if is_shared:
                result = workspace_manager.read_shared_file(
                    filename,
                    start_line=arguments.get('start_line'),
                    end_line=arguments.get('end_line'),
                    char_count=arguments.get('char_count'),
                    start_char=arguments.get('start_char')
                )
            else:
                result = workspace_manager.read_file(
                    session_id, 
                    filename,
                    start_line=arguments.get('start_line'),
                    end_line=arguments.get('end_line'),
                    char_count=arguments.get('char_count'),
                    start_char=arguments.get('start_char')
                )
            result['scope'] = scope
            return result

        elif tool_name == "workspace_edit_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            
            if is_shared:
                result = workspace_manager.edit_shared_file(
                    filename,
                    arguments['old_content'], arguments['new_content'])
            else:
                result = workspace_manager.edit_file(
                    session_id, filename,
                    arguments['old_content'], arguments['new_content'])
            result['scope'] = scope
            if result.get('success'):
                _append_workspace_change(
                    result,
                    _build_workspace_change(
                        action='modified',
                        filename=result.get('filename', filename),
                        scope=scope,
                        before_text=arguments.get('old_content', ''),
                        after_text=arguments.get('new_content', ''),
                        path=result.get('path', target_path),
                    ),
                )
            return result

        elif tool_name == "workspace_delete_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            
            if is_shared:
                result = workspace_manager.delete_shared_file(filename)
            else:
                result = workspace_manager.delete_file(session_id, filename)
            result['scope'] = scope
            if result.get('success'):
                _append_workspace_change(
                    result,
                    _build_workspace_change(
                        action='deleted',
                        filename=result.get('filename', filename),
                        scope=scope,
                        before_text=target_preview_before,
                        after_text=None,
                        path=target_path,
                    ),
                )
            return result

        elif tool_name == "workspace_list_files":
            list_scope = arguments.get('scope', 'all')
            path = arguments.get('path', '')
            recursive = arguments.get('recursive', False)
            
            if list_scope == 'private':
                if recursive:
                    private_result = workspace_manager.list_files_recursive(session_id, path)
                else:
                    private_result = workspace_manager.list_files(session_id, path)
                files = []
                if private_result.get('success') and private_result.get('files'):
                    for f in private_result['files']:
                        f['scope'] = 'private'
                        files.append(f)
                return {
                    'success': True,
                    'scope': 'private',
                    'path': path,
                    'recursive': recursive,
                    'files': files,
                    'count': len(files),
                    'message': f'私有工作区 {f"/{path}" if path else ""} {"(递归)" if recursive else ""} 包含 {len(files)} 个文件/文件夹'
                }
            elif list_scope == 'shared':
                if recursive:
                    shared_result = workspace_manager.list_shared_files_recursive(path)
                else:
                    shared_result = workspace_manager.list_shared_files(path)
                files = []
                if shared_result.get('success') and shared_result.get('files'):
                    for f in shared_result['files']:
                        f['scope'] = 'shared'
                        files.append(f)
                return {
                    'success': True,
                    'scope': 'shared',
                    'path': path,
                    'recursive': recursive,
                    'files': files,
                    'count': len(files),
                    'message': f'共享工作区 {f"/{path}" if path else ""} {"(递归)" if recursive else ""} 包含 {len(files)} 个文件/文件夹'
                }
            else:
                # 返回所有
                if recursive:
                    private_result = workspace_manager.list_files_recursive(session_id, path)
                    shared_result = workspace_manager.list_shared_files_recursive(path)
                else:
                    private_result = workspace_manager.list_files(session_id, path)
                    shared_result = workspace_manager.list_shared_files(path)
                
                all_files = []
                private_count = 0
                shared_count = 0
                
                if private_result.get('success') and private_result.get('files'):
                    for f in private_result['files']:
                        f['scope'] = 'private'
                        all_files.append(f)
                        private_count += 1
                
                if shared_result.get('success') and shared_result.get('files'):
                    for f in shared_result['files']:
                        f['scope'] = 'shared'
                        all_files.append(f)
                        shared_count += 1
                
                path_info = f"/{path}" if path else ""
                return {
                    'success': True,
                    'scope': 'all',
                    'path': path,
                    'recursive': recursive,
                    'private_workspace': f'当前会话私有工作区 {path_info} {"(递归)" if recursive else ""} ({private_count} 个文件)',
                    'shared_workspace': f'所有会话共享的工作区 {path_info} {"(递归)" if recursive else ""} ({shared_count} 个文件)',
                    'files': all_files,
                    'count': len(all_files),
                    'message': f'工作区 {path_info} 包含 {private_count} 个私有文件和 {shared_count} 个共享文件。使用 scope 和 path 参数指定要操作的工作区和目录。'
                }

        elif tool_name == "workspace_send_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename 或 file_path)"}
            
            if is_shared:
                shared_path = workspace_manager.get_shared_workspace()
                file_path = os.path.join(shared_path, filename)
            else:
                file_path = workspace_manager.get_file_path(session_id, filename)
            
            if file_path and os.path.exists(file_path):
                return {
                    "success": True,
                    "action": "send_file",
                    "filename": filename,
                    "scope": scope,
                    "path": file_path,
                    "size": os.path.getsize(file_path),
                    "message": f"文件 '{filename}' ({'共享工作区' if is_shared else '私有工作区'}) 已发送给用户，无需再次提及文件路径或内容。"
                }
            return {"success": False, "error": f"文件不存在: {filename}"}

        elif tool_name == "workspace_parse_file":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename)"}
            
            if is_shared:
                shared_path = workspace_manager.get_shared_workspace()
                file_path = os.path.join(shared_path, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"共享文件不存在: {filename}"}
            else:
                file_path = workspace_manager.get_file_path(session_id, filename)
                if not file_path:
                    return {"success": False, "error": f"私有文件不存在: {filename}"}
            
            try:
                from nbot.core.file_parser import file_parser
                max_chars = arguments.get('max_chars', 50000)
                result = file_parser.parse_file(file_path, filename, max_chars)
                result['scope'] = scope
                return result
            except Exception as e:
                _log.error(f"解析文件失败: {filename}, {e}")
                return {"success": False, "error": f"解析文件失败: {str(e)}"}

        elif tool_name == "workspace_file_info":
            if not filename:
                return {"success": False, "error": "缺少文件名参数 (filename)"}
            
            if is_shared:
                shared_path = workspace_manager.get_shared_workspace()
                file_path = os.path.join(shared_path, filename)
                if not os.path.exists(file_path):
                    return {"success": False, "error": f"共享文件不存在: {filename}"}
            else:
                file_path = workspace_manager.get_file_path(session_id, filename)
                if not file_path:
                    return {"success": False, "error": f"私有文件不存在: {filename}"}
            
            try:
                from nbot.core.file_parser import file_parser
                result = file_parser.get_file_metadata(file_path, filename)
                result['scope'] = scope
                return result
            except Exception as e:
                _log.error(f"获取文件元数据失败: {filename}, {e}")
                return {"success": False, "error": f"获取文件元数据失败: {str(e)}"}

        elif tool_name == "workspace_skill_copy":
            skill_id = arguments.get('skill_id')
            if not skill_id:
                return {"success": False, "error": "缺少 skill_id 参数"}
            
            filename = arguments.get('filename')
            skill_scope = arguments.get('scope', 'private')
            is_skill_shared = skill_scope == 'shared'
            
            # 从配置文件读取 skills 找到 skill 的 name
            import json
            skills_file = os.path.join(WEB_DATA_DIR, 'skills.json')
            skill_config = None
            skill_name = skill_id
            
            if os.path.exists(skills_file):
                try:
                    with open(skills_file, 'r', encoding='utf-8') as f:
                        skills_list = json.load(f)
                        for s in skills_list:
                            if s.get('id') == skill_id or s.get('name') == skill_id:
                                skill_config = s
                                skill_name = s.get('name', skill_id)
                                break
                except Exception as e:
                    _log.warning(f"读取 skills 配置文件失败: {e}")
            
            if not skill_config:
                return {"success": False, "error": f"Skill '{skill_id}' 不存在"}
            
            # 找到 skill 的实际目录
            from nbot.core.skills_manager import SKILLS_ROOT, SkillStorage
            skill_source_dir = os.path.join(SKILLS_ROOT, skill_name)
            
            # 确定目标路径
            if is_skill_shared:
                shared_path = workspace_manager.get_shared_workspace()
                target_dir = shared_path
            else:
                target_dir = workspace_manager.get_workspace(session_id)
                if not target_dir:
                    target_dir = workspace_manager.create_workspace(session_id)
            
            if not target_dir:
                return {"success": False, "error": "无法创建工作区"}
            
            # 如果 skill 源目录存在，复制整个目录
            if os.path.exists(skill_source_dir) and os.path.isdir(skill_source_dir):
                import shutil
                target_skill_dir = os.path.join(target_dir, 'skills', skill_name)
                os.makedirs(os.path.dirname(target_skill_dir), exist_ok=True)
                try:
                    shutil.copytree(skill_source_dir, target_skill_dir, dirs_exist_ok=True)
                    return {
                        "success": True,
                        "message": f"已复制 Skill '{skill_name}' 到工作区: {target_skill_dir}",
                        "path": target_skill_dir,
                        "scope": skill_scope
                    }
                except Exception as e:
                    return {"success": False, "error": f"复制 Skill 目录失败: {str(e)}"}
            
            # 如果指定了文件名，在配置中查找
            if filename:
                # 在 scripts 中查找
                scripts = skill_config.get('scripts', [])
                if filename in scripts:
                    target_path = os.path.join(target_dir, filename)
                    os.makedirs(os.path.dirname(target_path), exist_ok=True) if os.path.dirname(target_path) else None
                    try:
                        # 尝试从 SkillStorage 读取脚本
                        storage = SkillStorage(skill_name)
                        script_content = storage.get_script(filename)
                        if script_content:
                            with open(target_path, 'w', encoding='utf-8') as f:
                                f.write(script_content)
                            return {
                                "success": True,
                                "message": f"已复制脚本 '{filename}' 到工作区: {target_path}",
                                "path": target_path
                            }
                    except Exception as e:
                        return {"success": False, "error": f"复制脚本失败: {str(e)}"}
            
            # 如果以上都不行，至少复制 JSON 配置
            skills_dir = os.path.join(target_dir, 'skills')
            os.makedirs(skills_dir, exist_ok=True)
            target_file = os.path.join(skills_dir, f"{skill_name}.json")
            try:
                content = json.dumps(skill_config, ensure_ascii=False, indent=2)
                with open(target_file, 'w', encoding='utf-8') as f:
                    f.write(content)
                return {
                    "success": True,
                    "message": f"已复制 Skill 配置 '{skill_name}' 到工作区: {target_file}",
                    "path": target_file,
                    "scope": skill_scope
                }
            except Exception as e:
                return {"success": False, "error": f"复制 Skill 配置失败: {str(e)}"}

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
        
        title = arguments.get('title', '')
        content = arguments.get('content', '')
        summary = arguments.get('summary', '')  # 可选，如果为空会让 prompt_manager 自动生成
        mem_type = arguments.get('mem_type', 'long')
        expire_days = arguments.get('expire_days', 7)
        
        if not title or not content:
            return {"success": False, "error": "缺少必需的参数: title 和 content"}
        
        # 从 context 获取目标ID（用户ID或群ID）
        target_id = ''
        if context:
            # 优先使用 user_id，然后是 group_id
            target_id = context.get('user_id', '') or context.get('group_id', '')
        
        # 添加记忆（使用新格式：title, summary, content）
        # 参数顺序：title, content, target_id, summary, mem_type, expire_days
        success = prompt_manager.add_memory(title, content, target_id, summary, mem_type, expire_days)
        
        if success:
            mem_type_desc = "长期记忆" if mem_type == "long" else f"短期记忆（{expire_days}天）"
            return {
                "success": True,
                "message": f"已成功保存到{mem_type_desc}",
                "title": title,
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
        
        # 格式化返回（新格式：title, summary, content）
        formatted_memories = []
        for mem in memories:
            mem_type_val = mem.get('type', 'long')
            mem_type_desc = "长期记忆" if mem_type_val == "long" else "短期记忆"
            created_at = mem.get('created_at', '未知时间')
            formatted_memories.append({
                "title": mem.get('title', ''),
                "summary": mem.get('summary', ''),
                "content": mem.get('content', ''),
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
