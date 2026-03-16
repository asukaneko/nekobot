from nbot.plugins.skills.base import BaseSkill, SkillContext, SkillResult, SkillRegistry
from typing import Dict, Any
import logging

_log = logging.getLogger(__name__)


class DownloadComicSkill(BaseSkill):
    """漫画下载技能"""

    name = "download_comic"
    description = "下载漫画，支持输入漫画ID进行下载"
    aliases = ["下载漫画", "下漫画", "jm"]

    parameters = {
        "type": "object",
        "properties": {
            "comic_id": {
                "type": "string",
                "description": "漫画ID"
            }
        },
        "required": ["comic_id"]
    }

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """执行漫画下载"""
        comic_id = kwargs.get("comic_id", "")

        if not comic_id:
            return SkillResult(
                success=False,
                error="请提供漫画ID"
            )

        return SkillResult(
            success=True,
            content=f"[漫画下载] 正在下载漫画 {comic_id}...",
            data={
                "comic_id": comic_id,
                "status": "pending"
            }
        )


class DownloadNovelSkill(BaseSkill):
    """小说下载技能"""

    name = "download_novel"
    description = "下载轻小说，支持输入小说标题进行搜索和下载"
    aliases = ["下载小说", "下小说", "找小说"]

    parameters = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "小说标题"
            }
        },
        "required": ["title"]
    }

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """执行小说下载"""
        title = kwargs.get("title", "")

        if not title:
            return SkillResult(
                success=False,
                error="请提供小说标题"
            )

        return SkillResult(
            success=True,
            content=f"[小说下载] 正在搜索小说: {title}...",
            data={
                "title": title,
                "status": "searching"
            }
        )


class DownloadVideoSkill(BaseSkill):
    """视频下载技能"""

    name = "download_video"
    description = "下载视频，支持B站、西瓜视频等平台"
    aliases = ["下载视频", "下视频", "dv"]

    parameters = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "视频链接"
            }
        },
        "required": ["url"]
    }

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """执行视频下载"""
        url = kwargs.get("url", "")

        if not url:
            return SkillResult(
                success=False,
                error="请提供视频链接"
            )

        return SkillResult(
            success=True,
            content=f"[视频下载] 正在下载视频...",
            data={
                "url": url,
                "status": "downloading"
            }
        )


def register_builtin_download_skills():
    """注册下载相关技能"""
    SkillRegistry.register(DownloadComicSkill())
    SkillRegistry.register(DownloadNovelSkill())
    SkillRegistry.register(DownloadVideoSkill())
    _log.info("Download skills registered")
