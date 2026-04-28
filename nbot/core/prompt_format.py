from typing import Any, Dict, List


def format_memory_items(memories: List[Dict[str, str]]) -> str:
    """Format remembered topics for injection into a system prompt."""
    if not memories:
        return ""

    lines = [
        "## 可用记忆主题",
        "以下是可参考的长期记忆摘要，仅在与当前对话相关时使用：",
        "",
    ]
    for memory in memories:
        title = memory.get("title", "").strip()
        summary = memory.get("summary", "").strip()
        if title and summary:
            lines.append(f"- **{title}**: {summary}")
        elif title:
            lines.append(f"- **{title}**")
        elif summary:
            lines.append(f"- {summary}")

    return "\n\n" + "\n".join(lines).rstrip() + "\n"


def format_skills_prompt(skills: List[Dict[str, Any]]) -> str:
    """Format enabled Skills as a consistent system-prompt section."""
    enabled_skills = [skill for skill in skills if skill.get("enabled", True)]

    lines = [
        "## 可用技能 (Skills)",
        "当用户明确需要某项能力时，可使用下列 Skills；如需确认能力边界，先查询技能详情。",
        "",
    ]

    if enabled_skills:
        for skill in enabled_skills:
            name = skill.get("name", "").strip()
            description = skill.get("description", "").strip()
            aliases = [alias for alias in skill.get("aliases", []) if alias]

            if not name:
                continue

            lines.append(f"### {name}")
            if description:
                lines.append(f"- 描述：{description}")
            if aliases:
                lines.append(f"- 别名：{', '.join(aliases)}")
            lines.append("")
    else:
        lines.extend(["（暂无可用技能）", ""])

    lines.extend(
        [
            "### 使用规则",
            "1. 当用户需要使用或了解某个技能时，使用 `skill_get_info` 工具获取该技能的详细信息。",
            "2. 使用 `skill_list` 工具可以列出所有可用的 Skills。",
        ]
    )

    return "\n\n" + "\n".join(lines).rstrip() + "\n"
