import asyncio
import logging
from typing import Optional, List, Dict, Any
from nbot.core.prompt_format import format_skills_prompt
from nbot.plugins.skills.base import SkillContext, SkillRegistry

_log = logging.getLogger(__name__)


class SkillDispatcher:
    """Skill 调度器 - 用于在聊天过程中调用 Skills"""

    def __init__(self, plugin_manager):
        self.plugin_manager = plugin_manager

    def get_available_skills_prompt(self) -> str:
        """生成可用技能的描述，用于注入到 system prompt"""
        skills = self.plugin_manager.get_skill_list()

        if not skills:
            return ""

        skills_desc = format_skills_prompt(skills)
        skills_desc += """

### 技能调用格式
1. 当用户请求需要使用技能时，明确调用对应的技能。
2. 技能调用格式：`[SKILL:skill_name]参数[/SKILL]`。
3. 示例：`[SKILL:search]天气[/SKILL]`。

### 技能存储工具
- `skill_view`：查看指定 Skill 的文件结构，列出所有文件。
- `skill_read`：读取 Skill 存储空间中的指定文件内容。
"""

        return skills_desc

    async def parse_and_execute(
        self,
        response_text: str,
        context: SkillContext
    ) -> tuple[str, List[Dict[str, Any]]]:
        """解析 AI 回复中的技能调用并执行"""
        import re

        skill_results = []

        pattern = r'\[SKILL:(\w+)\](.*?)\[/SKILL\]'
        matches = re.findall(pattern, response_text, re.DOTALL)

        if not matches:
            return response_text, skill_results

        _log.info(f"Found {len(matches)} skill calls in response")

        for skill_name, params in matches:
            _log.info(f"Executing skill: {skill_name} with params: {params}")

            result = await self.plugin_manager.execute_skill(
                skill_name,
                context,
                message=params.strip()
            )

            if result.success:
                skill_results.append({
                    "skill": skill_name,
                    "success": True,
                    "content": result.content,
                    "data": result.data
                })
                response_text = response_text.replace(
                    f"[SKILL:{skill_name}]{params}[/SKILL]",
                    result.content
                )
            else:
                skill_results.append({
                    "skill": skill_name,
                    "success": False,
                    "error": result.error
                })
                response_text = response_text.replace(
                    f"[SKILL:{skill_name}]{params}[/SKILL]",
                    f"[技能执行失败: {result.error}]"
                )

            await asyncio.sleep(0.1)

        return response_text, skill_results

    def should_use_skill(self, message: str) -> Optional[str]:
        """判断消息是否触发了某个技能"""
        enabled_skills = SkillRegistry.get_enabled()

        message_lower = message.lower()

        for skill_name, skill in enabled_skills.items():
            if skill_name in message_lower:
                return skill_name

            for alias in skill.aliases:
                if alias in message:
                    return skill_name

        return None


skill_dispatcher: Optional[SkillDispatcher] = None


def get_skill_dispatcher(plugin_manager) -> SkillDispatcher:
    """获取 Skill 调度器"""
    global skill_dispatcher
    if skill_dispatcher is None:
        skill_dispatcher = SkillDispatcher(plugin_manager)
    return skill_dispatcher
