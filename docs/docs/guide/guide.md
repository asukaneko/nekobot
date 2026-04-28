# 开发指南

## 项目概述

NekoBot 是一个面向 QQ 与 Web 双频道的 AI 机器人，采用"统一 AI 内核 + 频道适配层"的架构设计：

- **统一 AI 核心** - QQ 与 Web 共用同一套 AI 处理链路
- **频道适配层** - 不同频道的输入输出归一化
- **工作区支持** - 私有和共享工作区，支持文件操作
- **工具调用** - 支持多轮工具调用和继续执行
- **Web 后台** - 完整的可视化管理界面

## 核心架构

```
Ncatbot-comic-QQbot/
├── bot.py                 # 入口文件，负责启动配置
├── nbot/                  # 核心模块包
│   ├── channels/          # 频道适配层
│   │   ├── base.py        # 频道适配器基类
│   │   ├── qq.py          # QQ 频道适配器
│   │   ├── web.py         # Web 频道适配器
│   │   ├── telegram.py    # Telegram 频道适配器
│   │   └── registry.py    # 频道注册器
│   │
│   ├── core/              # 统一 AI 核心
│   │   ├── agent_service.py   # AI 处理入口
│   │   ├── chat_models.py     # ChatRequest / ChatResponse 模型
│   │   ├── session_store.py   # 会话存储
│   │   ├── model_adapter.py   # 模型适配层
│   │   ├── workflow.py        # 工作流引擎
│   │   ├── message.py         # 消息模型
│   │   ├── skills_manager.py  # 技能管理器
│   │   └── todo_card.py       # 待办卡片
│   │
│   ├── plugins/           # 插件系统
│   │   ├── skills/        # 技能模块
│   │   │   ├── base.py        # 技能基类
│   │   │   ├── loader.py      # 技能加载器
│   │   │   └── builtin/       # 内置技能
│   │   ├── dispatcher.py      # 技能调度器
│   │   └── manager.py         # 插件管理器
│   │
│   ├── services/          # 服务层
│   │   ├── ai.py              # AI 客户端
│   │   ├── chat_service.py    # 聊天服务
│   │   ├── tools.py           # 工具注册
│   │   ├── skills_tools.py    # 技能工具
│   │   ├── todo_tools.py      # 待办工具
│   │   ├── react.py           # ReAct 模式
│   │   ├── stt.py             # 语音转文字
│   │   └── tts.py             # 文字转语音
│   │
│   ├── web/               # Web 界面
│   │   ├── server.py          # Flask 服务
│   │   ├── routes/            # API 路由
│   │   ├── static/            # 静态资源
│   │   └── templates/         # 前端模板
│   │
│   ├── commands.py        # 命令处理兼容层
│   ├── chat.py            # 聊天兼容层
│   ├── config.py          # 配置兼容层
│   └── heartbeat.py       # 心跳兼容层
│
├── data/                  # 运行数据
│   ├── qq/               # QQ 相关数据
│   ├── web/              # Web 会话、模型配置
│   ├── skills/           # Skills 存储
│   └── workspaces/       # 私有 / 共享工作区
│
└── resources/            # 静态资源
    ├── config/           # 配置文件
    └── prompts/          # 提示词
```

## 主要模块详解

### 1. 频道适配层 (nbot/channels/)

频道适配层负责将不同频道的输入输出归一化为统一的 `ChatRequest` / `ChatResponse`。

#### 基类定义 (base.py)

```python
from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities

class MyChannelAdapter(BaseChannelAdapter):
    channel_name = "my_channel"

    def get_capabilities(self):
        return ChannelCapabilities(
            supports_stream=True,
            supports_file_send=True,
            supports_progress_updates=True,
            supports_stop=True
        )

    def normalize_inbound_message(self, content: str) -> str:
        # 归一化输入消息
        return content.strip()

    def build_chat_request(self, **kwargs):
        # 构建 ChatRequest
        return super().build_chat_request(**kwargs)
```

#### 注册适配器

```python
from nbot.channels.registry import register_channel_adapter

register_channel_adapter("my_channel", MyChannelAdapter)
```

### 2. 统一 AI 核心 (nbot/core/)

#### ChatRequest / ChatResponse (chat_models.py)

```python
from nbot.core.chat_models import ChatRequest, ChatResponse

# 创建请求
request = ChatRequest(
    channel="web",
    conversation_id="session_123",
    user_id="user_456",
    content="你好",
    attachments=[],
    metadata={}
)

# 创建响应
response = ChatResponse(
    final_content="你好！有什么可以帮助你的？",
    can_continue=False,  # 是否可以继续执行
    tool_trace=[]        # 工具调用记录
)
```

#### Agent 服务 (agent_service.py)

```python
from nbot.core.agent_service import AgentService

agent = AgentService()

# 注册处理器
agent.register_handler("web", my_handler)

# 处理请求
response = agent.process(request)
```

#### 会话存储 (session_store.py)

```python
from nbot.core.session_store import get_session_store

store = get_session_store()

# 保存会话
store.save_session("session_id", messages)

# 加载会话
messages = store.load_session("session_id")
```

### 3. 服务层 (nbot/services/)

#### AI 客户端 (ai.py)

```python
from nbot.services.ai import ai_client

# 发送消息
response = await ai_client.chat(messages)

# 支持流式输出
async for chunk in ai_client.chat_stream(messages):
    yield chunk
```

#### 工具注册 (tools.py)

```python
from nbot.services.tools import register_tool

@register_tool("search")
async def search_tool(query: str) -> str:
    """搜索工具"""
    # 实现搜索逻辑
    return result
```

### 4. 插件系统 (nbot/plugins/)

#### 技能基类 (skills/base.py)

```python
from nbot.plugins.skills.base import Skill, skill

@skill
class MySkill(Skill):
    name = "my_skill"
    aliases = ["我的技能"]
    description = "技能描述"

    async def execute(self, content: str, **kwargs) -> str:
        # 技能执行逻辑
        return f"执行结果: {content}"
```

#### 动态技能 (dynamic_skill.py)

```python
from nbot.plugins.skills.dynamic_skill import DynamicSkill

skill = DynamicSkill(
    name="dynamic_skill",
    code="async def execute(self, content): return content.upper()"
)
```

### 5. 工作区 (Workspace)

工作区分为两种：

- **private** - 当前会话私有
- **shared** - 全局共享

```python
from nbot.core.workspace import get_workspace_manager

wm = get_workspace_manager()

# 获取私有工作区
private_ws = wm.get_private_workspace(session_id)

# 获取共享工作区
shared_ws = wm.get_shared_workspace()

# 文件操作
await private_ws.write_file("test.txt", "内容")
content = await private_ws.read_file("test.txt")
files = await private_ws.list_files()
```

## 命令系统

### 注册命令

在 `nbot/commands.py` 中使用装饰器注册命令：

```python
from nbot.commands import register_command

@register_command("/hello", help_text="/hello -> 你好")
async def handle_hello(msg, is_group=True):
    await msg.reply(text="你好！")

@register_command("/test", help_text="/test -> 测试命令", admin_show=True)
async def handle_test(msg, is_group=True):
    # 管理员命令
    await msg.reply(text="测试成功")
```

### 命令参数

```python
@register_command("/echo", help_text="/echo <内容> -> 回声")
async def handle_echo(msg, is_group=True):
    content = msg.raw_message.replace("/echo", "").strip()
    await msg.reply(text=content)
```

## Web 后台开发

### 添加 API 路由

在 `nbot/web/routes/` 中添加新路由：

```python
# nbot/web/routes/my_feature.py
from flask import Blueprint, jsonify

bp = Blueprint('my_feature', __name__)

@bp.route('/api/my-feature', methods=['GET'])
def get_my_feature():
    return jsonify({"status": "ok"})
```

### 注册路由

在 `nbot/web/server.py` 中注册：

```python
from nbot.web.routes import my_feature

app.register_blueprint(my_feature.bp)
```

## 扩展开发建议

### 添加新频道

1. 在 `nbot/channels/` 创建适配器类
2. 继承 `BaseChannelAdapter`
3. 实现必要的方法
4. 在 `registry.py` 中注册

### 添加新工具

1. 在 `nbot/services/tools.py` 中使用 `@register_tool` 装饰器
2. 实现工具函数
3. 在 Web 后台配置工具权限

### 添加新技能

1. 在 `nbot/plugins/skills/` 创建技能类
2. 继承 `Skill` 基类或使用 `@skill` 装饰器
3. 实现 `execute` 方法

## 配置说明

### 环境变量 (.env)

```env
# 必需
WEB_PASSWORD=your_password

# QQ 配置
BOT_UIN=123456
ROOT=789012
WS_URI=ws://localhost:3001
TOKEN=napcat_token
WEBUI_URI=http://localhost:6099

# AI 配置（可在 Web 端配置）
```

### 模型配置

在 Web 后台的 AI 配置中心可以配置：

- API 密钥
- 基础 URL
- 模型名称
- 能力声明（supports_tools, supports_reasoning, supports_stream）

## 相关链接

- [NapCat 文档](https://napneko.github.io)
- [ncatbot 文档](https://docs.ncatbot.xyz/)
