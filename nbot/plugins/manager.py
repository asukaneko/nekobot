import logging
from typing import Dict, Any, Optional, List
from nbot.plugins.skills.base import SkillRegistry, SkillContext, SkillResult
from nbot.plugins.skills.loader import SkillLoader
from nbot.plugins.skills.builtin import register_all_builtin_skills
from nbot.plugins.dispatcher import SkillDispatcher, get_skill_dispatcher

_log = logging.getLogger(__name__)


class PluginManager:
    """插件/技能管理器"""

    def __init__(self):
        self.skill_loader = SkillLoader()
        self._initialized = False
        self._dispatcher = None

    def initialize(self):
        """初始化管理器"""
        if self._initialized:
            _log.warning("PluginManager already initialized")
            return

        register_all_builtin_skills()
        self.skill_loader.load_all()

        self._initialized = True
        _log.info("PluginManager initialized")

    def get_skill_dispatcher(self, plugin_manager) -> SkillDispatcher:
        """获取技能调度器"""
        if self._dispatcher is None:
            self._dispatcher = get_skill_dispatcher(plugin_manager)
        return self._dispatcher

    async def execute_skill(
        self,
        skill_name: str,
        context: SkillContext,
        **kwargs
    ) -> SkillResult:
        """执行技能（优先使用 Web 配置的动态执行）"""
        # 使用混合执行器，优先从 Web 配置执行
        from nbot.plugins.skills.dynamic_skill import get_hybrid_executor

        try:
            hybrid_executor = get_hybrid_executor()
            return await hybrid_executor.execute(skill_name, context, **kwargs)
        except Exception as e:
            _log.error(f"Skill execution failed: {e}")
            return SkillResult(
                success=False,
                error=f"执行失败: {str(e)}"
            )

    async def match_and_execute(
        self,
        message: str,
        context: SkillContext,
        **kwargs
    ) -> Optional[SkillResult]:
        """匹配并执行技能"""
        enabled_skills = SkillRegistry.get_enabled()

        for skill_name, skill in enabled_skills.items():
            for alias in [skill_name] + skill.aliases:
                if alias in message:
                    _log.info(f"Matched skill: {skill_name}")
                    return await self.execute_skill(
                        skill_name,
                        context,
                        message=message,
                        **kwargs
                    )

        return None

    def get_skill_list(self) -> List[Dict[str, Any]]:
        """获取技能列表"""
        return SkillRegistry.list_skills()

    def enable_skill(self, skill_name: str) -> bool:
        """启用技能"""
        skill = SkillRegistry.get(skill_name)
        if skill:
            skill.enabled = True
            _log.info(f"Enabled skill: {skill_name}")
            return True
        return False

    def disable_skill(self, skill_name: str) -> bool:
        """禁用技能"""
        skill = SkillRegistry.get(skill_name)
        if skill:
            skill.enabled = False
            _log.info(f"Disabled skill: {skill_name}")
            return True
        return False

    def reload_external_skills(self) -> int:
        """重新加载外部技能"""
        return self.skill_loader.load_all()


plugin_manager: Optional[PluginManager] = None


def get_plugin_manager() -> PluginManager:
    """获取插件管理器单例"""
    global plugin_manager
    if plugin_manager is None:
        plugin_manager = PluginManager()
        plugin_manager.initialize()
    return plugin_manager
