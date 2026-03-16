# Plugins Module
from nbot.plugins.skills.base import BaseSkill, SkillContext, SkillResult, SkillRegistry
from nbot.plugins.skills.loader import SkillLoader
from nbot.plugins.manager import PluginManager, get_plugin_manager
from nbot.plugins.dispatcher import SkillDispatcher, get_skill_dispatcher

__all__ = [
    "BaseSkill",
    "SkillContext",
    "SkillResult",
    "SkillRegistry",
    "SkillLoader",
    "PluginManager",
    "get_plugin_manager",
    "SkillDispatcher",
    "get_skill_dispatcher",
]
