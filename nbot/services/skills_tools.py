"""
Skills 工具模块 - 提供 AI 访问 Skills 存储的功能

使用装饰器自动注册工具，AI 可以调用以下工具：
- skill_list: 列出所有 Skills 的存储空间
- skill_view: 查看指定 Skill 的详细信息，列出所有文件
- skill_read: 读取 Skill 存储空间中的指定文件内容
- skill_get_info: 获取 Skills 系统信息
"""
import logging
import os
from typing import Dict, Any
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
    return {
        "name": skill.get('name', ''),
        "description": skill.get('description', ''),
        "files": files,
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
    description="查看指定 Skill 的文件结构，列出所有文件及目录。用于了解 Skill 包含哪些文件。",
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
def skill_view(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """查看 Skill 文件结构"""
    try:
        skill_name = args.get('skill_name', '')

        info = _get_skills_storage_info()
        manager = info["manager"]

        if not manager.skill_exists(skill_name):
            return {
                "success": False,
                "error": f"Skill '{skill_name}' 的存储空间不存在"
            }

        storage = manager.get_skill_storage(skill_name)
        files = storage.get_all_files()

        result = {
            "success": True,
            "skill_name": skill_name,
            "files": files,
            "files_count": len(files)
        }

        return result

    except Exception as e:
        _log.error(f"skill_view error: {e}")
        return {"success": False, "error": f"查看 Skill 失败: {str(e)}"}


@register_tool(
    name="skill_read",
    description="读取 Skill 存储空间中的指定文件内容。支持按行范围或字符范围读取。",
    parameters={
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "Skill 的名称"
            },
            "file_path": {
                "type": "string",
                "description": "文件路径，可以是 SKILL.md、reference.md、scripts/main.py、resources/config.json 等"
            },
            "start_line": {
                "type": "integer",
                "description": "开始行号（从1开始），与 end_line 配合使用可读取指定行范围。例如：start_line=10, end_line=20 表示读取第10到20行。"
            },
            "end_line": {
                "type": "integer",
                "description": "结束行号（包含），需要与 start_line 配合使用。例如：start_line=10, end_line=20 表示读取第10到20行。"
            },
            "char_count": {
                "type": "integer",
                "description": "读取的字符数量，从文件开头或 start_char 指定位置开始。与 start_char 配合可从任意位置读取指定长度。"
            },
            "start_char": {
                "type": "integer",
                "description": "从第几个字符开始读取（从0开始）。需要与 char_count 配合使用。例如：start_char=100, char_count=200 表示从第100个字符开始读取200个字符。"
            }
        },
        "required": ["skill_name", "file_path"]
    }
)
def skill_read(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """读取 Skill 存储空间中的文件"""
    try:
        skill_name = args.get('skill_name', '')
        file_path_param = args.get('file_path', '')
        start_line = args.get('start_line')
        end_line = args.get('end_line')
        char_count = args.get('char_count')
        start_char = args.get('start_char')

        info = _get_skills_storage_info()
        storage = info["manager"].get_skill_storage(skill_name)

        if not os.path.exists(storage.skill_dir):
            return {
                "success": False,
                "error": f"Skill '{skill_name}' 不存在"
            }

        file_path = os.path.join(storage.skill_dir, file_path_param)

        if not os.path.exists(file_path):
            return {
                "success": False,
                "error": f"文件 '{file_path_param}' 不存在"
            }

        if not os.path.isfile(file_path):
            return {
                "success": False,
                "error": f"'{file_path_param}' 是目录而非文件"
            }

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            return {
                "success": False,
                "error": f"文件 '{file_path_param}' 是二进制文件，无法读取"
            }

        if start_line is not None or end_line is not None:
            lines = content.split('\n')
            total_lines = len(lines)

            start = (start_line - 1) if start_line else 0
            end = end_line if end_line else total_lines

            start = max(0, start)
            end = min(total_lines, end)

            if start >= total_lines:
                return {
                    "success": False,
                    "error": f"开始行号 {start_line} 超出文件行数（文件共 {total_lines} 行）"
                }

            selected_content = '\n'.join(lines[start:end])
            return {
                "success": True,
                "skill_name": skill_name,
                "file_path": file_path_param,
                "content": selected_content,
                "total_lines": total_lines,
                "read_lines": f"{start + 1}-{end}",
                "total_length": len(content),
                "read_length": len(selected_content),
                "read_mode": "line_range"
            }

        if start_char is not None or char_count is not None:
            start = start_char if start_char else 0
            count = char_count if char_count else len(content) - start

            if start >= len(content):
                return {
                    "success": False,
                    "error": f"开始位置 {start_char} 超出文件长度（文件共 {len(content)} 字符）"
                }

            end = min(start + count, len(content))
            selected_content = content[start:end]

            return {
                "success": True,
                "skill_name": skill_name,
                "file_path": file_path_param,
                "content": selected_content,
                "total_length": len(content),
                "read_length": len(selected_content),
                "read_range": f"{start}-{end}",
                "read_mode": "char_range"
            }

        MAX_CONTENT_LENGTH = 8000
        result = {
            "success": True,
            "skill_name": skill_name,
            "file_path": file_path_param,
            "total_length": len(content),
            "read_mode": "auto_truncate"
        }

        if len(content) > MAX_CONTENT_LENGTH:
            first_line = content.split('\n')[0] if content else ""
            result["content"] = content[:MAX_CONTENT_LENGTH]
            result["truncated"] = True
            result["summary"] = f"文件共 {len(content)} 字符，已截断显示前 {MAX_CONTENT_LENGTH} 字符。"
            result["first_line"] = first_line[:200] if first_line else ""
            result["read_more"] = "如需读取更多内容，请使用 start_line/end_line 或 start_char/char_count 参数。"
        else:
            result["content"] = content
            result["truncated"] = False

        return result

    except Exception as e:
        _log.error(f"skill_read error: {e}")
        return {"success": False, "error": f"读取文件失败: {str(e)}"}


@register_tool(
    name="skill_get_info",
    description="获取 Skills 系统信息，包括所有可用的 Skills 列表及其描述。"
)
def skill_get_info(args: Dict, context: Dict = None) -> Dict[str, Any]:
    """获取所有可用的 Skills 信息"""
    try:
        info = _get_skills_storage_info()
        all_skills = info["skills"]

        skills_list = []

        for skill in all_skills:
            skills_list.append({
                "name": skill.get('name', ''),
                "description": skill.get('description', ''),
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
