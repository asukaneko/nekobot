# 开发指南

## 项目概述

NekoBot 是一个基于 NapCat 的 QQ 机器人，采用模块化设计，主要功能包括：
- 📚 漫画下载
- 💬 AI 聊天
- 🎵 多媒体处理
- 🧠 记忆系统
- 📚 知识库
- 🌐 Web 界面

## 核心架构

```
Ncatbot-comic-QQbot/
├── bot.py                    # 入口文件
├── nbot/                     # 核心模块包
│   ├── commands.py          # 命令处理
│   ├── chat.py              # 聊天兼容层
│   ├── config.py            # 配置兼容层
│   ├── heartbeat.py          # 心跳兼容层
│   │
│   ├── core/                 # 核心功能
│   │   ├── heartbeat.py      # 心跳模块
│   │   ├── memory.py         # 记忆系统
│   │   ├── knowledge.py      # 知识库 (RAG)
│   │   └── workflow.py       # 工作流引擎
│   │
│   ├── plugins/             # 插件系统
│   │   ├── skills/          # 技能模块
│   │   │   ├── base.py      # 技能基类
│   │   │   ├── loader.py     # 技能加载器
│   │   │   └── builtin/     # 内置技能
│   │   │       ├── search.py
│   │   │       └── download.py
│   │   ├── dispatcher.py     # 技能调度器
│   │   └── manager.py        # 插件管理器
│   │
│   ├── services/            # 服务层
│   │   ├── ai.py           # AI 客户端
│   │   ├── chat_service.py  # 聊天服务
│   │   ├── tts.py           # 语音合成
│   │   └── react.py         # 反应模块
│   │
│   ├── utils/               # 工具模块
│   │   └── config_loader.py  # 配置加载器
│   │
│   └── web/                 # Web 界面
│       ├── server.py        # Flask 服务
│       └── templates/        # 前端模板
│
├── resources/               # 静态资源
│   ├── config/             # 配置文件
│   └── prompts/            # 提示词
│
└── tools/                   # 工具脚本
```

## 主要模块

### 1. 命令系统 (nbot/commands.py)

使用装饰器注册命令：

```python
from nbot.commands import register_command

@register_command("/hello", help_text="/hello -> 你好")
async def handle_hello(msg, is_group=True):
    await msg.reply(text="你好！")
```

### 2. 聊天服务 (nbot/services/chat_service.py)

```python
from nbot.services.chat_service import chat, load_prompt

# 聊天
response = chat(content="你好", user_id="123456")

# 加载提示词
prompt = load_prompt(user_id="123456", group_id=None)
```

### 3. AI 客户端 (nbot/services/ai.py)

```python
from nbot.services.ai import ai_client, user_messages, group_messages

# 发送消息
user_messages["123456"] = [
    {"role": "system", "content": "你是猫娘"},
    {"role": "user", "content": "你好"}
]

# 调用 AI
response = await ai_client.chat(user_messages["123456"])
```

### 4. 插件系统 (nbot/plugins/)

```python
from nbot.plugins.skills.base import Skill, skill

class MySkill(Skill):
    name = "my_skill"
    aliases = ["我的技能"]
    
    async def execute(self, content: str) -> str:
        return f"执行技能: {content}"
```

### 5. 记忆系统 (nbot/core/memory.py)

```python
from nbot.core.memory import get_memory_manager

mm = get_memory_manager()

# 添加记忆
mm.remember(content="用户喜欢猫娘", user_id="123456")

# 搜索记忆
results = mm.recall(user_id="123456", query="喜欢")
```

### 6. 知识库 (nbot/core/knowledge.py)

```python
from nbot.core.knowledge import get_knowledge_manager

km = get_knowledge_manager()

# 创建知识库
kb = km.create_knowledge_base(name="测试库", user_id="123456")

# 添加文档
km.add_document(kb.id, title="FAQ", content="常见问题解答")

# 搜索
results = km.search(query="问题", user_id="123456")
```

### 7. 工作流 (nbot/core/workflow.py)

```python
from nbot.core.workflow import get_workflow_engine

engine = get_workflow_engine()

# 执行工作流
result = await engine.execute_workflow("workflow_name", {"data": "value"})
```

## Web 界面

启动 Web 服务：

```bash
python bot.py --web
```

访问 `http://localhost:5000`

## 配置说明

| 文件 | 说明 |
|------|------|
| `config.ini` | 账号、API 密钥 |
| `resources/config/option.yml` | 漫画下载配置 |
| `resources/config/urls.ini` | 图片 API |
| `resources/prompts/neko.txt` | AI 角色提示词 |

## 扩展开发

### 添加新命令

在 `nbot/commands.py` 中使用装饰器：

```python
@register_command("/新命令", help_text="/新命令 -> 说明")
async def handle_new_command(msg, is_group=True):
    await msg.reply(text="命令处理逻辑")
```

### 添加新技能

在 `nbot/plugins/skills/` 中创建新技能类。

## 相关链接

- [ncatbot 文档](https://docs.ncatbot.xyz/)
- [NapCat 文档](https://napneko.github.io)
- [JMComic 文档](https://jmcomic.readthedocs.io/zh-cn/latest/)
