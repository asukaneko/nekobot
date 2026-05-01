"""
动态 Skill - 从 Web 配置执行
"""
from typing import Any, Dict
import logging

from nbot.plugins.skills.base import BaseSkill, SkillContext, SkillResult
from nbot.services.dynamic_executor import get_executor

_log = logging.getLogger(__name__)


class DynamicSkill(BaseSkill):
    """
    动态 Skill - 根据 Web 配置执行
    """

    def __init__(self, config: Dict[str, Any]):
        """
        从配置初始化 Skill

        Args:
            config: Skill 配置，包含 name, description, aliases, parameters, implementation
        """
        self.config = config
        self.name = config.get('name', '')
        self.description = config.get('description', '')
        self.aliases = config.get('aliases', [])
        self.parameters = config.get('parameters', {})
        self.enabled = config.get('enabled', True)
        self.implementation = config.get('implementation', {})

        # 获取执行器
        self._executor = get_executor()

    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """
        执行 Skill

        Args:
            context: 执行上下文
            **kwargs: 用户传入的参数

        Returns:
            SkillResult: 执行结果
        """
        try:
            # 准备上下文
            exec_context = {
                'user_id': context.user_id,
                'group_id': context.group_id,
                'message': context.message,
                'sender_name': context.sender_name,
            }

            # 使用动态执行器执行
            result = self._executor.execute_skill(
                self.config,
                kwargs,
                exec_context
            )

            # 转换为 SkillResult
            if result.get('success'):
                return SkillResult(
                    success=True,
                    content=result.get('content', str(result.get('data', ''))),
                    data=result.get('data', {}),
                    usage=result.get('usage', {})
                )
            else:
                return SkillResult(
                    success=False,
                    error=result.get('error', '执行失败'),
                    content=f"执行失败: {result.get('error', '未知错误')}"
                )

        except Exception as e:
            _log.error(f"Dynamic skill execution failed: {e}")
            return SkillResult(
                success=False,
                error=str(e),
                content=f"执行失败: {str(e)}"
            )


class HybridSkillExecutor:
    """
    混合 Skill 执行器
    优先使用动态配置，如果没有则使用内置 Skill
    """

    def __init__(self):
        self._executor = get_executor()

    async def execute(self, skill_name: str, context: SkillContext, **kwargs) -> SkillResult:
        """
        执行 Skill（优先使用动态配置）

        Args:
            skill_name: Skill 名称或别名
            context: 执行上下文
            **kwargs: 参数

        Returns:
            SkillResult: 执行结果
        """
        # 1. 先从 Web 配置查找
        from nbot.plugins.skills.base import load_skills_config
        web_config = load_skills_config()

        skill_config = None
        for config in web_config:
            if config.get('name') == skill_name or skill_name in config.get('aliases', []):
                skill_config = config
                break

        # 2. 如果找到 Web 配置且有 implementation，使用动态执行
        if skill_config and skill_config.get('implementation'):
            _log.info(f"Executing dynamic skill: {skill_name}")
            dynamic_skill = DynamicSkill(skill_config)
            return await dynamic_skill.execute(context, **kwargs)

        # 3. 否则使用内置 Skill
        from nbot.plugins.skills.base import SkillRegistry
        skill = SkillRegistry.get(skill_name)

        if skill:
            _log.info(f"Executing built-in skill: {skill_name}")
            return await skill.execute(context, **kwargs)

        # 4. 未找到 Skill
        return SkillResult(
            success=False,
            error=f"Skill not found: {skill_name}",
            content=f"未找到技能: {skill_name}"
        )


# 全局混合执行器
_hybrid_executor = None


def get_hybrid_executor() -> HybridSkillExecutor:
    """获取全局混合执行器"""
    global _hybrid_executor
    if _hybrid_executor is None:
        _hybrid_executor = HybridSkillExecutor()
    return _hybrid_executor
