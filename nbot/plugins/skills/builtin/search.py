from nbot.plugins.skills.base import BaseSkill, SkillContext, SkillResult, SkillRegistry
from typing import Dict, Any
import logging

_log = logging.getLogger(__name__)


class SearchSkill(BaseSkill):
    """搜索技能 - 使用阿里云 OpenSearch 获取最新信息"""

    name = "search"
    description = "搜索互联网获取最新信息，适用于询问天气、新闻、实时数据等需要最新信息的问题"
    aliases = ["搜索", "查找", "联网搜索"]

    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词"
            }
        },
        "required": ["query"]
    }

    def __init__(self):
        super().__init__()
        self._client = None
        self._api_key = None
        self._api_url = None

    def _get_client(self):
        """获取搜索客户端"""
        if self._client is None:
            try:
                import configparser
                config_parser = configparser.ConfigParser()
                config_parser.read('config.ini', encoding='utf-8')
                self._api_key = config_parser.get('search', 'api_key')
                self._api_url = config_parser.get('search', 'api_url')
            except Exception as e:
                _log.error(f"Failed to load search config: {e}")
        return self._client

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """执行搜索"""
        query = kwargs.get("query", context.message)

        if not query:
            return SkillResult(
                success=False,
                error="搜索关键词不能为空"
            )

        if not self._api_key or not self._api_url:
            return SkillResult(
                success=False,
                error="搜索服务未配置"
            )

        try:
            import requests

            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}"
            }
            data = {
                "query": query,
                "query_rewrite": True,
                "top_k": 6
            }

            response = requests.post(
                self._api_url,
                headers=headers,
                json=data,
                timeout=10
            )
            response.raise_for_status()

            result = response.json()
            search_results = str(result.get("result", {}).get("search_result", []))

            return SkillResult(
                success=True,
                content=search_results,
                data={
                    "query": query,
                    "result_count": len(search_results)
                }
            )

        except Exception as e:
            _log.error(f"Search failed: {e}")
            return SkillResult(
                success=False,
                error=f"搜索失败: {str(e)}"
            )


class ImageSearchSkill(BaseSkill):
    """图片搜索技能 - 搜索相关图片"""

    name = "image_search"
    description = "搜索相关图片，适用于需要展示图片的场景"
    aliases = ["搜图", "找图"]

    parameters = {
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "图片关键词"
            }
        },
        "required": ["keyword"]
    }

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """执行图片搜索"""
        keyword = kwargs.get("keyword", context.message)

        if not keyword:
            return SkillResult(
                success=False,
                error="图片关键词不能为空"
            )

        return SkillResult(
            success=True,
            content=f"[图片搜索] 关键词: {keyword}",
            data={"keyword": keyword}
        )


def register_builtin_skills():
    """注册内置技能"""
    SkillRegistry.register(SearchSkill())
    SkillRegistry.register(ImageSearchSkill())
    _log.info("Built-in skills registered")
