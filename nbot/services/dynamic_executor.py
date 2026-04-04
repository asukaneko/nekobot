"""
动态执行引擎 - 支持从配置执行 Skills 和 Tools
支持类型：http、python、static、minimax_web_search
"""
import json
import logging
import re
import requests
import datetime
from typing import Dict, Any, Optional

_log = logging.getLogger(__name__)


class DynamicExecutor:
    """动态执行器 - 根据配置执行各种操作"""
    
    def __init__(self):
        self.config_cache = {}
    
    def execute_skill(self, skill_config: Dict, params: Dict, context: Dict = None) -> Dict[str, Any]:
        """
        执行 Skill
        
        Args:
            skill_config: Skill 配置（包含 implementation）
            params: 用户传入的参数
            context: 上下文信息（如 user_id, group_id 等）
        
        Returns:
            执行结果
        """
        implementation = skill_config.get('implementation', {})
        impl_type = implementation.get('type', 'static')
        
        try:
            if impl_type == 'http':
                return self._execute_http(implementation, params, context)
            elif impl_type == 'python':
                return self._execute_python(implementation, params, context)
            elif impl_type == 'static':
                return self._execute_static(implementation, params, context)
            elif impl_type == 'minimax_web_search':
                return self._execute_minimax_search(implementation, params, context)
            elif impl_type == 'builtin':
                return self._execute_builtin(implementation, params, context)
            else:
                return {
                    'success': False,
                    'error': f'Unknown implementation type: {impl_type}'
                }
        except Exception as e:
            _log.error(f"Skill execution failed: {e}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def execute_tool(self, tool_config: Dict, arguments: Dict, context: Dict = None) -> Dict[str, Any]:
        """
        执行 Tool
        
        Args:
            tool_config: Tool 配置（包含 implementation）
            arguments: 工具参数
            context: 上下文信息
        
        Returns:
            执行结果
        """
        # Tools 和 Skills 使用相同的执行逻辑
        return self.execute_skill(tool_config, arguments, context)
    
    def _execute_http(self, implementation: Dict, params: Dict, context: Dict) -> Dict[str, Any]:
        """执行 HTTP 请求"""
        method = implementation.get('method', 'GET')
        url_template = implementation.get('url', '')
        headers_template = implementation.get('headers', {})
        body_template = implementation.get('body')
        response_path = implementation.get('response_path', '')
        transform = implementation.get('transform')
        max_length = implementation.get('max_length', 5000)
        error_message = implementation.get('error_message', '请求失败')
        
        # 准备变量替换
        variables = self._prepare_variables(params, context)
        
        # 替换 URL
        url = self._render_template(url_template, variables)
        if not url:
            return {'success': False, 'error': 'URL is empty'}
        
        # 替换 Headers
        headers = {}
        for key, value in headers_template.items():
            headers[key] = self._render_template(str(value), variables)
        
        # 替换 Body
        body = None
        if body_template:
            if isinstance(body_template, dict):
                body = {}
                for key, value in body_template.items():
                    if isinstance(value, str):
                        body[key] = self._render_template(value, variables)
                    else:
                        body[key] = value
            else:
                body = self._render_template(str(body_template), variables)
        
        # 发送请求
        try:
            if method.upper() == 'GET':
                response = requests.get(url, headers=headers, timeout=30)
            elif method.upper() == 'POST':
                if isinstance(body, dict):
                    response = requests.post(url, headers=headers, json=body, timeout=30)
                else:
                    response = requests.post(url, headers=headers, data=body, timeout=30)
            else:
                return {'success': False, 'error': f'Unsupported HTTP method: {method}'}
            
            response.raise_for_status()
            
            # 解析响应
            try:
                data = response.json()
            except:
                data = {'text': response.text[:max_length]}
            
            # 提取指定路径的数据
            if response_path:
                result = self._extract_path(data, response_path)
            else:
                result = data
            
            # 数据转换
            if transform and isinstance(result, dict):
                result = self._transform_data(result, transform, variables)
            
            return {
                'success': True,
                'data': result,
                'raw_response': data if len(str(data)) < max_length else str(data)[:max_length] + '...'
            }
            
        except requests.exceptions.RequestException as e:
            _log.error(f"HTTP request failed: {e}")
            return {
                'success': False,
                'error': f"{error_message}: {str(e)}"
            }
    
    def _execute_python(self, implementation: Dict, params: Dict, context: Dict) -> Dict[str, Any]:
        """执行 Python 代码（受限环境）"""
        code = implementation.get('code', '')
        
        # 只允许简单的表达式，不允许执行危险操作
        # 这里使用受限的 eval/exec
        allowed_globals = {
            'datetime': datetime,
            'json': json,
            're': re,
            'params': params,
            'context': context,
        }
        
        try:
            # 使用 eval 执行简单表达式
            result = eval(code, {"__builtins__": {}}, allowed_globals)
            return {
                'success': True,
                'data': result,
                'content': str(result)
            }
        except Exception as e:
            _log.error(f"Python execution failed: {e}")
            return {
                'success': False,
                'error': f'执行失败: {str(e)}'
            }
    
    def _execute_static(self, implementation: Dict, params: Dict, context: Dict) -> Dict[str, Any]:
        """返回静态响应"""
        response_template = implementation.get('response', '')
        variables = self._prepare_variables(params, context)
        content = self._render_template(response_template, variables)
        
        return {
            'success': True,
            'content': content,
            'data': {'message': content}
        }
    
    def _execute_minimax_search(self, implementation: Dict, params: Dict, context: Dict) -> Dict[str, Any]:
        """执行 Web Search（使用搜狗搜索，无需 API key），并抓取页面正文"""
        query = params.get('query', '')
        num_results = params.get('num_results', 3)

        if not query:
            return {'success': False, 'error': 'Query is empty'}

        try:
            from bs4 import BeautifulSoup
            from concurrent.futures import ThreadPoolExecutor, as_completed

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }

            search_url = "https://www.sogou.com/web"
            search_params = {"query": query, "num": str(num_results * 2)}

            response = requests.get(search_url, headers=headers, params=search_params, timeout=15)
            response.encoding = 'utf-8'

            soup = BeautifulSoup(response.text, 'html.parser')
            vrwraps = soup.find_all('div', class_='vrwrap')

            results = []
            for vr in vrwraps:
                if len(results) >= num_results:
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

                    if href.startswith('/link'):
                        href = 'https://www.sogou.com' + href

                    snippet = ""
                    for tag in vr.find_all(['p', 'div', 'span']):
                        text = tag.get_text(strip=True)
                        if len(text) > len(snippet) and len(text) > 30 and text != title:
                            snippet = text

                    results.append({
                        "title": title,
                        "snippet": snippet[:500],
                        "url": href,
                        "content": ""
                    })
                except Exception:
                    continue

            if not results:
                return {'success': False, 'error': '未找到搜索结果'}

            # 并发抓取页面正文
            def resolve_sogou_url(url: str) -> str:
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
                except Exception:
                    return (url, "")

            with ThreadPoolExecutor(max_workers=num_results) as executor:
                future_map = {
                    executor.submit(fetch_page_content, r['url']): i
                    for i, r in enumerate(results) if r['url']
                }
                for future in as_completed(future_map):
                    idx = future_map[future]
                    try:
                        real_url, content = future.result()
                        results[idx]['url'] = real_url
                        results[idx]['content'] = content
                    except Exception:
                        pass

            return {
                'success': True,
                'query': query,
                'results': results,
                'count': len(results)
            }

        except requests.exceptions.RequestException as e:
            _log.error(f"Sogou search request failed: {e}")
            return {
                'success': False,
                'error': f'搜索请求失败: {str(e)}'
            }
        except Exception as e:
            _log.error(f"Sogou search failed: {e}")
            return {
                'success': False,
                'error': f'搜索失败: {str(e)}'
            }
    
    def _execute_builtin(self, implementation: Dict, params: Dict, context: Dict) -> Dict[str, Any]:
        """执行内置工具"""
        handler = implementation.get('handler', '')
        
        try:
            if handler == 'exec_command':
                from nbot.services.tools import ToolExecutor
                command = params.get('command', '')
                timeout = params.get('timeout', 30)
                confirmed = params.get('confirmed', False)
                return ToolExecutor.exec_command(command, timeout=timeout, confirmed=confirmed)
            elif handler == 'download_file':
                from nbot.services.tools import ToolExecutor
                url = params.get('url', '')
                filename = params.get('filename')
                workspace_id = params.get('workspace_id')
                # 如果没有提供 workspace_id，尝试从 context 获取
                if not workspace_id and context.get('session_id'):
                    workspace_id = context.get('session_id')
                return ToolExecutor.download_file(url, filename=filename, workspace_id=workspace_id)
            elif handler == 'send_message':
                from nbot.services.tools import ToolExecutor
                content = params.get('content', '')
                message_type = params.get('message_type', 'info')
                session_id = context.get('session_id')
                return ToolExecutor.send_message(content, message_type=message_type, session_id=session_id)
            else:
                return {
                    'success': False,
                    'error': f'Unknown builtin handler: {handler}'
                }
        except Exception as e:
            _log.error(f"Builtin execution failed: {e}")
            return {
                'success': False,
                'error': f'执行失败: {str(e)}'
            }
    
    def _prepare_variables(self, params: Dict, context: Dict) -> Dict[str, str]:
        """准备模板变量"""
        variables = {}
        
        # 添加参数
        if params:
            for key, value in params.items():
                variables[key] = str(value) if value is not None else ''
        
        # 添加上下文
        if context:
            for key, value in context.items():
                variables[key] = str(value) if value is not None else ''
        
        # 添加配置变量（从 config.ini 读取）
        try:
            import configparser
            config = configparser.ConfigParser()
            config.read('config.ini', encoding='utf-8')
            
            # API Keys
            variables['minimax_api_key'] = config.get('ApiKey', 'api_key', fallback='')
            variables['minimax_model'] = config.get('ApiKey', 'model', fallback='MiniMax-Text-01')
            variables['minimax_base_url'] = config.get('ApiKey', 'base_url', fallback='')
            
            # Search API
            variables['search_api_key'] = config.get('search', 'api_key', fallback='')
            variables['search_api_url'] = config.get('search', 'api_url', fallback='')
            
            # Bot Config
            variables['bot_uin'] = config.get('BotConfig', 'bot_uin', fallback='')
            
        except Exception as e:
            _log.error(f"Failed to load config variables: {e}")
        
        return variables
    
    def _render_template(self, template: str, variables: Dict) -> str:
        """渲染模板字符串，支持 {{var}} 和 ${var} 语法"""
        if not template:
            return ''
        
        if not variables:
            return template
        
        try:
            # 使用正则表达式替换 {{var}} 格式的变量
            import re
            result = template
            for key, value in variables.items():
                # 替换 {{key}} 格式
                result = result.replace('{{' + key + '}}', str(value))
                # 替换 ${key} 格式
                result = result.replace('${' + key + '}', str(value))
            return result
        except Exception as e:
            _log.error(f"Template rendering failed: {e}")
            return template
    
    def _extract_path(self, data: Any, path: str) -> Any:
        """从数据中提取指定路径的值"""
        if not path:
            return data
        
        keys = path.split('.')
        result = data
        
        for key in keys:
            if isinstance(result, dict):
                result = result.get(key)
            elif isinstance(result, list):
                try:
                    index = int(key)
                    result = result[index] if 0 <= index < len(result) else None
                except (ValueError, IndexError):
                    return None
            else:
                return None
            
            if result is None:
                return None
        
        return result
    
    def _transform_data(self, data: Dict, transform: Dict, variables: Dict) -> Dict:
        """转换数据格式"""
        result = {}
        for key, template in transform.items():
            if isinstance(template, str):
                # 如果是模板字符串，先尝试从 data 中提取，再渲染模板
                if template.startswith('{{') and template.endswith('}}'):
                    path = template[2:-2].strip()
                    value = self._extract_path(data, path)
                    result[key] = value if value is not None else ''
                else:
                    result[key] = self._render_template(template, {**variables, **data})
            else:
                result[key] = template
        return result


# 全局执行器实例
_executor = None


def get_executor() -> DynamicExecutor:
    """获取全局执行器实例"""
    global _executor
    if _executor is None:
        _executor = DynamicExecutor()
    return _executor
