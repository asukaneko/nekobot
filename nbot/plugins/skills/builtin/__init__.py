from nbot.plugins.skills.builtin.search import register_builtin_skills
from nbot.plugins.skills.builtin.download import register_builtin_download_skills


def register_all_builtin_skills():
    """注册所有内置技能"""
    register_builtin_skills()
    register_builtin_download_skills()


__all__ = ["register_all_builtin_skills"]
