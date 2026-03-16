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
from string import Template

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
        """执行 MiniMax Web Search"""
        api_key_template = implementation.get('api_key', '')
        model_template = implementation.get('model', 'MiniMax-Text-01')
        
        variables = self._prepare_variables(params, context)
        api_key = self._render_template(api_key_template, variables)
        model = self._render_template(model_template, variables)
        query = params.get('query', '')
        
        if not api_key:
            return {'success': False, 'error': 'MiniMax API key not configured'}
        
        if not query:
            return {'success': False, 'error': 'Query is empty'}
        
        try:
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}"
            }
            
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是一个 helpful assistant。"},
                    {"role": "user", "content": query}
                ],
                "tools": [{"type": "web_search"}],
                "max_tokens": 4096
            }
            
            response = requests.post(
                "https://api.minimaxi.com/v1/text/chatcompletion_v2",
                headers=headers,
                json=payload,
                timeout=30
            )
            response.raise_for_status()
            
            result = response.json()
            
            # 提取搜索结果
            choices = result.get('choices', [])
            if choices and 'messages' in choices[0]:
                messages = choices[0]['messages']
                for msg in messages:
                    if msg.get('role') == 'tool':
                        return {
                            'success': True,
                            'content': msg.get('content', ''),
                            'data': result
                        }
            
            return {
                'success': True,
                'content': '搜索完成',
                'data': result
            }
            
        except Exception as e:
            _log.error(f"MiniMax search failed: {e}")
            return {
                'success': False,
                'error': f'搜索失败: {str(e)}'
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
        """渲染模板字符串"""
        if not template:
            return ''
        
        try:
            # 使用 string.Template 进行变量替换
            t = Template(template)
            return t.safe_substitute(variables)
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
