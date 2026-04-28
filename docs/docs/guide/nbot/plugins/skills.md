# skills - 技能系统

## 概述

`skills` 模块提供技能插件系统，允许动态扩展机器人的功能。技能可以被 AI 调用，实现自定义的业务逻辑。

## 技能结构

```
nbot/plugins/skills/
├── base.py           # 技能基类
├── loader.py         # 技能加载器
├── builtin/          # 内置技能
│   ├── search.py
│   ├── weather.py
│   └── ...
└── dynamic_skill.py  # 动态技能
```

## 创建技能

### 继承 Skill 基类

```python
from nbot.plugins.skills.base import Skill, skill

@skill
class MySkill(Skill):
    name = "my_skill"                 # 技能标识
    aliases = ["我的技能"]             # 别名
    description = "技能描述"           # 描述
    parameters = {                     # 参数定义
        "query": {
            "type": "string",
            "description": "搜索关键词"
        }
    }

    async def execute(self, content: str, **kwargs) -> str:
        """执行技能"""
        query = kwargs.get("query")
        # 实现技能逻辑
        return f"搜索结果: {query}"
```

### 使用装饰器注册

```python
from nbot.plugins.skills.base import register_skill

@register_skill("custom_skill")
async def custom_skill_handler(content: str, **kwargs) -> str:
    """自定义技能处理器"""
    return "处理结果"
```

## 动态技能

支持在运行时创建和修改技能：

```python
from nbot.plugins.skills.dynamic_skill import DynamicSkill

# 创建动态技能
skill = DynamicSkill(
    name="dynamic_search",
    code="""
async def execute(self, content, **kwargs):
    import requests
    query = kwargs.get('query', content)
    # 实现搜索逻辑
    return f"搜索: {query}"
"""
)

# 保存技能
skill.save()

# 执行技能
result = await skill.execute("搜索内容", query="关键词")
```

## 技能存储

技能存储在 `data/skills/` 目录：

```
data/skills/
├── my_skill/
│   ├── skill.json      # 技能配置
│   └── script.py       # 技能代码
└── another_skill/
    └── ...
```

## 技能配置

```json
{
    "name": "my_skill",
    "description": "技能描述",
    "version": "1.0.0",
    "author": "作者",
    "enabled": true,
    "parameters": {
        "query": {
            "type": "string",
            "required": true
        }
    }
}
```

## 技能工具集成

技能可以注册为 AI 可调用的工具：

```python
from nbot.services.tool_registry import register_tool

@register_tool("my_skill_tool")
def my_skill_tool(arguments: dict, context: dict = None) -> dict:
    """技能工具"""
    skill = load_skill("my_skill")
    result = skill.execute(**arguments)
    return {
        "success": True,
        "result": result
    }
```

## Web 管理

Web 后台提供技能管理界面：

- 查看所有技能
- 启用/禁用技能
- 编辑技能代码
- 测试技能执行
- 导入/导出技能

## 内置技能

| 技能名 | 说明 |
|--------|------|
| search | 搜索技能 |
| weather | 天气查询 |
| news | 新闻获取 |
| translate | 翻译 |
| calculate | 计算 |
