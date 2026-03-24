"""
Skills 工具模块 - 提供 AI 访问 Skills 存储的功能

使用装饰器自动注册工具，AI 可以调用以下工具：
- skill_list: 列出所有 Skills 的存储空间
- skill_view: 查看指定 Skill 的详细信息
- skill_list_scripts: 列出指定 Skill 的所有脚本
- skill_read_script: 读取 Skill 脚本内容
- skill_get_info: 获取 Skills 系统信息
"""
import logging
from typing import Dict, Any, List
from nbot.services.tool_registry import register_tool

_log = logging.getLogger(__name__)
_log.info("[SkillsTools] 模块已加载，开始注册工具...")


def _get_skills_storage_info() -> Dict[str, Any]:
    """获取 Skills 存储信息"""
    from nbot.core.skills_manager import get_skills_storage_manager
    manager = get_skills_storage_manager()
    return {
        "manager": manager,
        "skills": manager.list_skills()
    }


def _format_skill_info(skill: Dict) -> Dict:
    """格式化 Skill 信息"""
    files = skill.get('files', [])
    scripts = skill.get('scripts', [])
    return {
        "name": skill.get('name', ''),
        "description": skill.get('description', ''),
        "scripts": scripts,
        "files": files,
        "scripts_count": len(scripts),
        "files_count": len(files)
    }


@register_tool(
    name="skill_list",
    description="列出所有 Skills 的存储空间，查看哪些 Skills 有独立的存储目录和脚本文件。"
)
def skill_list(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """列出所有 Skills 存储"""
    try:
        info = _get_skills_storage_info()
        skills = info["skills"]

        if not skills:
            return {
                "success": True,
                "message": "暂无可用的 Skills 存储",
                "skills": []
            }

        return {
            "success": True,
            "message": f"找到 {len(skills)} 个 Skills 存储",
            "skills": [_format_skill_info(s) for s in skills]
        }

    except Exception as e:
        _log.error(f"skill_list error: {e}")
        return {"success": False, "error": f"获取 Skills 列表失败: {str(e)}"}


@register_tool(
    name="skill_view",
    description="查看指定 Skill 的详细信息，包括配置文件、脚本列表和内容。用于查看 Skill 的实现细节。",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill 的名称"
            },
            "file_name": {
                "type": "string",
                "description": "要查看的文件名（可选，如不填则显示 Skill 概览）"
            }
        },
        "required": ["skill_name"]
    }
)
def skill_view(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """查看 Skill 详情"""
    try:
        skill_name = args.get('skill_name', '')
        file_name = args.get('file_name')

        info = _get_skills_storage_info()
        manager = info["manager"]

        if not manager.skill_exists(skill_name):
            return {
                "success": False,
                "error": f"Skill '{skill_name}' 的存储空间不存在"
            }

        storage = manager.get_skill_storage(skill_name)

        result = {
            "success": True,
            "skill_name": skill_name,
            "scripts": storage.list_scripts(),
            "files": storage.get_all_files()
        }

        if file_name:
            content = storage.load_script(file_name)
            if content:
                result["file_content"] = content
                result["file_name"] = file_name
            else:
                result["error"] = f"文件 '{file_name}' 不存在"

        return result

    except Exception as e:
        _log.error(f"skill_view error: {e}")
        return {"success": False, "error": f"查看 Skill 失败: {str(e)}"}


@register_tool(
    name="skill_list_scripts",
    description="列出指定 Skill 的所有脚本文件。",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill 的名称"
            }
        },
        "required": ["skill_name"]
    }
)
def skill_list_scripts(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """列出 Skill 的脚本"""
    try:
        skill_name = args.get('skill_name', '')

        info = _get_skills_storage_info()
        storage = info["manager"].get_skill_storage(skill_name)
        scripts = storage.list_scripts()

        return {
            "success": True,
            "skill_name": skill_name,
            "scripts": scripts,
            "count": len(scripts)
        }

    except Exception as e:
        _log.error(f"skill_list_scripts error: {e}")
        return {"success": False, "error": f"列出脚本失败: {str(e)}"}


@register_tool(
    name="skill_read_script",
    description="读取 Skill 存储空间中的任意文件内容。",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill 的名称"
            },
            "script_name": {
                "type": "string",
                "description": "文件名，可以是 SKILL.md、reference.md、LICENSE.txt 或 scripts/main.py 等"
            }
        },
        "required": ["skill_name", "script_name"]
    }
)
def skill_read_script(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """读取 Skill 存储空间中的文件"""
    try:
        skill_name = args.get('skill_name', '')
        script_name = args.get('script_name', '')

        info = _get_skills_storage_info()
        storage = info["manager"].get_skill_storage(skill_name)

        # 构建完整文件路径
        import os
        file_path = os.path.join(storage.skill_dir, script_name)

        # 检查文件是否存在
        if not os.path.exists(file_path):
            # 尝试检查是否是 scripts/ 目录下的文件
            scripts_path = os.path.join(storage.scripts_dir, script_name)
            if os.path.exists(scripts_path):
                file_path = scripts_path
            else:
                return {
                    "success": False,
                    "error": f"文件 '{script_name}' 不存在"
                }

        # 读取文件内容
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            return {
                "success": False,
                "error": f"文件 '{script_name}' 是二进制文件，无法读取"
            }

        return {
            "success": True,
            "skill_name": skill_name,
            "script_name": script_name,
            "content": content
        }

    except Exception as e:
        _log.error(f"skill_read_script error: {e}")
        return {"success": False, "error": f"读取文件失败: {str(e)}"}


@register_tool(
    name="skill_get_info",
    description="获取 Skills 系统信息，包括所有可用的 Skills 列表及其描述。"
)
def skill_get_info(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """获取所有可用的 Skills 信息"""
    try:
        info = _get_skills_storage_info()
        manager = info["manager"]
        all_skills = info["skills"]

        skills_list = []

        for skill in all_skills:
            skills_list.append({
                "name": skill.get('name', ''),
                "description": skill.get('description', ''),
                "scripts": skill.get('scripts', []),
                "files_count": len(skill.get('files', [])),
                "source": "storage",
                "has_storage": True
            })

        return {
            "success": True,
            "total_count": len(skills_list),
            "skills": skills_list
        }

    except Exception as e:
        _log.error(f"skill_get_info error: {e}")
        return {"success": False, "error": f"获取 Skills 信息失败: {str(e)}"}
