import asyncio
import logging
from typing import Optional, List, Dict, Any
from nbot.plugins.skills.base import SkillContext, SkillResult, SkillRegistry

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

        skills_desc = "## 可用技能 (Skills)\n"
        skills_desc += "你可以使用以下技能来帮助用户：\n\n"

        for skill in skills:
            if not skill.get("enabled", True):
                continue

            skills_desc += f"### {skill['name']}\n"
            skills_desc += f"- 描述: {skill['description']}\n"
            if skill.get("aliases"):
                skills_desc += f"- 别名: {', '.join(skill['aliases'])}\n"
            skills_desc += "\n"

        skills_desc += """
**使用规则：**
1. 当用户请求需要使用技能时，明确调用对应的技能
2. 技能调用格式：`[SKILL:skill_name]参数[/SKILL]`
3. 例如：`[SKILL:search]天气[/SKILL]`

**查看技能详情：**
你可以使用以下工具来查看技能的详细信息和脚本：
- `skill_list`: 列出所有 Skills 的存储空间
- `skill_view`: 查看指定 Skill 的详细信息
- `skill_list_scripts`: 列出 Skill 的所有脚本
- `skill_read_script`: 读取 Skill 脚本内容
- `skill_get_info`: 获取所有可用的 Skills 信息

当用户询问某个技能的功能或需要查看技能的实现时，可以使用这些工具。
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
