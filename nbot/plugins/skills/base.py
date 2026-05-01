from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, List
from dataclasses import dataclass, field
import logging
import json
import os

_log = logging.getLogger(__name__)

# Web 配置数据目录
WEB_DATA_DIR = os.path.join(os.path.dirname(__file__), '..', '..', '..', 'data', 'web')


def load_skills_config() -> List[Dict]:
    """从 web 配置文件加载 skills 配置"""
    skills_file = os.path.join(WEB_DATA_DIR, 'skills.json')
    if os.path.exists(skills_file):
        try:
            with open(skills_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            _log.error(f"Failed to load skills config: {e}")
    return []


def get_skill_enabled_status(skill_name: str) -> bool:
    """获取技能的启用状态（从web配置）"""
    web_config = load_skills_config()
    if web_config:
        for skill in web_config:
            if skill.get('name') == skill_name:
                return skill.get('enabled', True)
    return True  # 默认启用


@dataclass
class SkillContext:
    """技能执行上下文"""
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    message: str = ""
    raw_message: str = ""
    sender_name: str = ""
    bot_api = None
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillResult:
    """技能执行结果"""
    success: bool
    content: str = ""
    data: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)


class BaseSkill(ABC):
    """Skill 基类"""

    name: str = ""
    description: str = ""
    aliases: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    @abstractmethod
    async def execute(self, context: SkillContext, **kwargs) -> SkillResult:
        """执行技能"""
        pass

    def get_description(self) -> str:
        """获取技能描述"""
        return self.description

    def get_schema(self) -> Dict[str, Any]:
        """获取技能的 JSON Schema"""
        return {
            "name": self.name,
            "description": self.description,
            "aliases": self.aliases,
            "parameters": self.parameters
        }

    async def validate_params(self, params: Dict[str, Any]) -> bool:
        """验证参数"""
        return True


class SkillRegistry:
    """Skill 注册表"""

    _skills: Dict[str, BaseSkill] = {}
    _aliases: Dict[str, str] = {}

    @classmethod
    def register(cls, skill: BaseSkill):
        """注册技能"""
        if skill.name in cls._skills:
            _log.warning(f"Skill '{skill.name}' already registered, overwriting")

        cls._skills[skill.name] = skill

        for alias in skill.aliases:
            if alias in cls._aliases:
                _log.warning(f"Alias '{alias}' already mapped to '{cls._aliases[alias]}', overwriting")
            cls._aliases[alias] = skill.name

        _log.info(f"Registered skill: {skill.name} (aliases: {skill.aliases})")

    @classmethod
    def get(cls, name: str) -> Optional[BaseSkill]:
        """获取技能"""
        if name in cls._skills:
            return cls._skills[name]

        if name in cls._aliases:
            skill_name = cls._aliases[name]
            return cls._skills.get(skill_name)

        return None

    @classmethod
    def get_all(cls) -> Dict[str, BaseSkill]:
        """获取所有技能"""
        return cls._skills.copy()

    @classmethod
    def get_enabled(cls) -> Dict[str, BaseSkill]:
        """获取已启用的技能（从web配置读取启用状态）"""
        enabled_skills = {}
        for name, skill in cls._skills.items():
            # 从web配置读取启用状态
            is_enabled = get_skill_enabled_status(name)
            if is_enabled:
                enabled_skills[name] = skill
        return enabled_skills

    @classmethod
    def unregister(cls, name: str):
        """注销技能"""
        if name in cls._skills:
            skill = cls._skills.pop(name)
            for alias in skill.aliases:
                cls._aliases.pop(alias, None)
            _log.info(f"Unregistered skill: {name}")

    @classmethod
    def list_skills(cls) -> List[Dict[str, Any]]:
        """列出所有技能（从web配置读取启用状态）"""
        skills = []
        for skill in cls._skills.values():
            # 从web配置读取启用状态
            is_enabled = get_skill_enabled_status(skill.name)
            skills.append({
                "name": skill.name,
                "description": skill.description,
                "aliases": skill.aliases,
                "enabled": is_enabled
            })
        return skills
