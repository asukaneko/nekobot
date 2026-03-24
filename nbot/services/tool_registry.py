"""
工具注册系统 - 使用装饰器简化工具注册流程

使用方式：
1. 定义工具执行函数
2. 使用 @register_tool 装饰器注册
3. 自动完成定义、执行器注册

示例：
    @register_tool(
        name="my_tool",
        description="这是一个示例工具",
        parameters={
            "type": "object",
            "properties": {
                "param1": {"type": "string", "description": "参数1"}
            },
            "required": ["param1"]
        }
    )
    def my_tool_handler(args, context):
        return {"success": True, "result": args["param1"]}
"""
import logging
from typing import Dict, Any, List, Callable, Optional
from functools import wraps

_log = logging.getLogger(__name__)


class ToolRegistry:
    """工具注册表"""

    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}

    def register(self, name: str, definition: Dict, executor: Callable):
        """注册工具"""
        self._tools[name] = {
            "definition": definition,
            "executor": executor
        }
        _log.debug(f"Registered tool: {name}")

    def get_definition(self, name: str) -> Optional[Dict]:
        """获取工具定义"""
        if name in self._tools:
            return self._tools[name]["definition"]
        return None

    def get_executor(self, name: str) -> Optional[Callable]:
        """获取工具执行器"""
        if name in self._tools:
            return self._tools[name]["executor"]
        return None

    def get_all_definitions(self) -> List[Dict]:
        """获取所有工具定义"""
        return [self._tools[name]["definition"] for name in self._tools]

    def execute(self, tool_name: str, arguments: Dict, context: Dict = None) -> Dict[str, Any]:
        """执行工具"""
        executor = self.get_executor(tool_name)
        if not executor:
            return {
                "success": False,
                "error": f"Tool '{tool_name}' not found"
            }

        try:
            return executor(arguments, context)
        except Exception as e:
            _log.error(f"Tool execution error: {tool_name} - {e}")
            return {
                "success": False,
                "error": str(e)
            }

    def unregister(self, name: str) -> bool:
        """注销工具"""
        if name in self._tools:
            del self._tools[name]
            _log.info(f"Unregistered tool: {name}")
            return True
        return False

    def list_tools(self) -> List[str]:
        """列出所有已注册的工具名称"""
        return list(self._tools.keys())


# 全局注册表实例
_registry = ToolRegistry()


def register_tool(
    name: str,
    description: str,
    parameters: Dict = None
):
    """
    工具注册装饰器

    Args:
        name: 工具名称
        description: 工具描述（AI 根据这个决定何时调用）
        parameters: JSON Schema 参数定义

    Returns:
        装饰器函数

    示例：
        @register_tool(
            name="search_code",
            description="搜索代码仓库中的代码",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"]
            }
        )
        def search_code_handler(args, context):
            # args: 传入的参数
            # context: 上下文信息（包含 session_id, user_id 等）
            return {"success": True, "results": [...]}
    """
    if parameters is None:
        parameters = {"type": "object", "properties": {}}

    def decorator(func: Callable):
        # 构建工具定义
        definition = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": parameters
            }
        }

        # 注册到全局注册表
        _registry.register(name, definition, func)

        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)

        return wrapper

    return decorator


def get_registry() -> ToolRegistry:
    """获取全局注册表"""
    return _registry


def get_all_tool_definitions() -> List[Dict]:
    """获取所有工具定义"""
    return _registry.get_all_definitions()


def execute_tool(tool_name: str, arguments: Dict, context: Dict = None) -> Dict[str, Any]:
    """执行工具"""
    return _registry.execute(tool_name, arguments, context)
