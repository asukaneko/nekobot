# 快速开始

> 一个面向 QQ 与 Web 双频道的 AI 机器人，集成聊天、工作区、工具调用、知识库、记忆、工作流与可视化管理后台

## 环境要求

::: warning
- Python 3.11+
- Windows / Linux / macOS
- 建议使用小号登录 QQ
:::

::: info
基于 [NapCat](https://github.com/NapNeko/NapCatQQ) 和 [ncatbot](https://github.com/NapNeko/NcatBot) 开发
:::

## 安装

### 1. 克隆项目

```bash
git clone https://github.com/asukaneko/comic-downloader.git
cd comic-downloader
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 NapCat

首次运行会自动提示下载 NapCat，或手动下载：

- [NapCat 下载](https://github.com/NapNeko/NapCatQQ/releases)
- 解压到根目录，重命名为 `napcat`

### 4. 配置环境变量

复制 `.env.example` 为 `.env` 并编辑：

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# Web 登录密码（必需）
WEB_PASSWORD=你的Web登录密码

# QQ 配置（可选，如果不需要 QQ 功能可跳过）
BOT_UIN=你的QQ号
ROOT=管理员QQ号
WS_URI=ws://localhost:3001
TOKEN=napcat_token
WEBUI_URI=http://localhost:6099

# AI 模型配置（可在 Web 端配置）
# 支持多模型配置，在 Web 后台的 AI 配置中心添加
```

::: tip
和 AI 相关的配置都可以在 Web 端进行配置，包括：
- 多模型配置管理
- API 密钥设置
- 模型能力声明（supports_tools, supports_reasoning, supports_stream）
:::

## 启动

### 同时启动 QQ + Web（默认）

```bash
python bot.py
```

### 仅启动 Web 后台

```bash
python bot.py --only-web
```

### 仅启动 QQ

```bash
python bot.py --no-web
```

### CLI + Web 模式

```bash
python bot.py --cli-and-web
```

### 自定义 Web 地址

```bash
python bot.py --web-host 0.0.0.0 --web-port 5000
```

## 访问 Web 后台

启动后访问 `http://localhost:5000`，使用 `.env` 中设置的 `WEB_PASSWORD` 登录。

Web 后台功能：
- **仪表盘** - 监控机器人状态、消息趋势和系统健康
- **聊天** - Web 端 AI 对话
- **会话管理** - 管理 Web 和 QQ 会话
- **AI 配置** - 模型配置、API 密钥管理
- **记忆管理** - 用户个性化记忆
- **知识库** - RAG 知识库管理
- **Skills 配置** - 技能插件管理
- **Tools 配置** - 工具配置
- **工作流** - 可视化工作流编排
- **定时任务** - 任务调度
- **Token 用量** - 用量统计
- **系统日志** - 日志查看
- **调试台** - 调试工具

## 项目结构

```
Ncatbot-comic-QQbot/
├── bot.py                 # 入口文件
├── nbot/                  # 核心模块包
│   ├── channels/          # 频道适配层（QQ / Web / Telegram）
│   │   ├── base.py        # 频道适配器基类
│   │   ├── qq.py          # QQ 频道适配器
│   │   ├── web.py         # Web 频道适配器
│   │   ├── telegram.py    # Telegram 频道适配器
│   │   └── registry.py    # 频道注册器
│   ├── core/              # 统一 AI 核心
│   │   ├── agent_service.py   # AI 处理入口
│   │   ├── chat_models.py     # ChatRequest / ChatResponse
│   │   ├── session_store.py   # 会话读写
│   │   ├── model_adapter.py   # 模型适配层
│   │   ├── workflow.py        # 工作流引擎
│   │   └── message.py         # 消息模型
│   ├── plugins/           # 插件系统
│   │   ├── skills/        # 技能模块
│   │   ├── dispatcher.py  # 技能调度器
│   │   └── manager.py     # 插件管理器
│   ├── services/          # AI、工具、聊天服务
│   │   ├── ai.py          # AI 客户端
│   │   ├── chat_service.py # 聊天服务
│   │   ├── tools.py       # 工具注册
│   │   └── react.py       # ReAct 模式
│   └── web/               # Web 后台与前端
│       ├── server.py      # Flask 服务
│       ├── routes/        # API 路由
│       ├── static/        # 静态资源
│       └── templates/     # 前端模板
├── data/                  # 运行数据
│   ├── qq/               # QQ 相关数据
│   ├── web/              # Web 会话、模型配置
│   ├── skills/           # Skills 存储
│   └── workspaces/       # 私有 / 共享工作区
└── resources/            # 静态资源
    ├── config/           # 配置文件
    └── prompts/          # 提示词
```

## 工作区说明

工作区分为两种：

- **private** - 当前会话私有，位于 `data/workspaces/private/<session_id>/`
- **shared** - 全局共享，位于 `data/workspaces/_shared/`

AI 可以通过工具调用来读写工作区文件，支持文件变更预览与 diff 展示。

## 配置文件说明

| 文件 | 说明 |
|------|------|
| `.env` | 环境变量配置（推荐） |
| `config.ini` | 兼容配置（兜底） |
| `resources/config/option.yml` | 漫画下载配置 |
| `resources/config/urls.ini` | 图片 API |
| `resources/prompts/neko.txt` | AI 角色提示词 |

## 下一步

- [命令手册](./commands.md) - 了解所有 QQ 命令
- [开发指南](./guide.md) - 开发自己的功能
- [频道管理](./channels.md) - 配置多频道接入
