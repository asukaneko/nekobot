# NekoBot

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11+-3776AB?style=flat&logo=python&logoColor=white)
![License](https://img.shields.io/badge/License-MIT-2ea043?style=flat)
![Web](https://img.shields.io/badge/Web-Dashboard-ec4899?style=flat)
![QQ](https://img.shields.io/badge/QQ-NapCat%20%2B%20ncatbot-58a6ff?style=flat)

一个面向 QQ 与 Web 双频道的 AI 机器人项目，集成聊天、工作区、工具调用、知识库、记忆、工作流与可视化管理后台。

</div>

---

## 项目简介

NekoBot 最初是一个面向 QQ 的机器人项目，现在已经演进为一套更完整的多频道 AI 助手系统：

- QQ 与 Web 共用同一套 AI 核心
- 支持会话级与共享工作区
- 支持工具调用、文件读写、搜索、知识库检索
- 提供 Web 仪表盘、AI 配置中心、工作流、日志与调试界面
- 支持保存多模型配置，并在运行时切换

如果你想要的是：

- 一个可以接入 NapCat 的 QQ 机器人
- 一个带 Web 管理后台的 AI 助手
- 一个支持工作区操作和多轮工具调用的项目骨架

这个项目已经具备这些能力。

---

## 当前能力

### 核心 AI 能力

- 统一的 `ChatRequest / ChatResponse` 处理链路
- 支持普通聊天与工具调用聊天
- 支持继续执行、上下文裁剪、消息历史管理
- 支持多模型配置与运行时切换
- 支持不同厂商模型的适配层

### Web 管理后台

- 仪表盘首页
- Web 会话与 QQ 会话管理
- AI 配置中心与模型卡片
- 工具、技能、知识库、记忆管理
- 工作流与定时任务
- Token 用量、日志、调试面板
- 文件变更卡片、进度卡片、步骤详情弹窗

### 工作区能力

- 私有工作区：当前会话独享
- 共享工作区：所有会话可访问
- 文件创建、修改、删除、发送
- 文件变更预览与 diff 展示

### QQ 侧能力

- 命令系统
- AI 对话
- 漫画相关功能
- 轻小说相关功能
- 定时任务与提醒
- 群聊和私聊场景支持

---

## 架构概览

当前项目已经从“QQ bot + Web 页面”演进为“统一 AI 内核 + 频道适配层”的结构。

### 启动层

- `bot.py`
  负责加载环境变量、注入运行时配置、启动 QQ 服务与 Web 服务。

### 统一核心层

- `nbot/core/chat_models.py`
  统一 `ChatRequest` / `ChatResponse`
- `nbot/core/agent_service.py`
  统一 AI 处理入口
- `nbot/core/session_store.py`
  统一会话读写
- `nbot/core/model_adapter.py`
  统一模型请求与响应适配

### 频道适配层

- `nbot/channels/base.py`
- `nbot/channels/qq.py`
- `nbot/channels/web.py`

QQ 与 Web 在这里完成输入输出归一化，而不是各自维护一套聊天内核。

### 业务实现层

- `nbot/services/`
  AI、工具、QQ 聊天主链
- `nbot/web/`
  Web 服务、路由、Socket 事件、前端模板

---

## 目录结构

```text
Ncatbot-comic-QQbot/
├─ bot.py
├─ config.ini
├─ .env.example
├─ requirements.txt
├─ nbot/
│  ├─ channels/          # 频道适配层（QQ / Web）
│  ├─ core/              # 统一 AI 核心
│  ├─ plugins/           # 插件
│  ├─ services/          # AI、工具、聊天服务
│  └─ web/               # Web 后台与前端
├─ data/
│  ├─ qq/                # QQ 相关运行数据
│  ├─ skills/            # Skills 存储
│  ├─ web/               # Web 会话、模型、配置数据
│  └─ workspaces/        # 私有 / 共享工作区
├─ docs/
│  ├─ README.md
│  ├─ Chinese.md
│  ├─ CHANGELOG.md
│  └─ docs/
└─ resources/
   ├─ config/
   └─ prompts/
```

---

## 环境要求

- Python 3.11+
- NapCat
- 可用的模型 API Key

推荐先准备好：

- NapCat 的 WebSocket 地址
- 机器人 QQ 号
- 管理员 QQ 号
- 模型 API Key

---

## 安装

### 1. 克隆仓库

```bash
git clone https://github.com/asukaneko/comic-downloader.git
cd comic-downloader
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

优先推荐使用 `.env`，项目启动时会读取 `.env` 并注入运行时配置，不会自动回写 `config.ini`。

你可以参考：

- [`.env.example`](./.env.example)
- [`config.ini`](./config.ini)

一个最小可运行示例：

```env
BOT_UIN=你的机器人QQ号
ROOT=管理员QQ号
WS_URI=ws://127.0.0.1:3001
TOKEN=你的NapCat令牌

API_KEY=你的模型API_KEY
BASE_URL=https://api.minimaxi.com/v1/text/chatcompletion_v2
MODEL=MiniMax-M2.7

WEB_PASSWORD=你的Web登录密码
```

---

## 启动方式

### 同时启动 QQ + Web

```bash
python bot.py
```

### 只启动 Web

```bash
python bot.py --only-web
```

### 禁用 Web，仅启动 QQ

```bash
python bot.py --no-web
```

### 自定义 Web 地址

```bash
python bot.py --web-host 0.0.0.0 --web-port 5000
```

---

## 配置说明

### 1. `.env` 与 `config.ini`

当前推荐策略：

- `.env` 作为运行时优先配置来源
- `config.ini` 作为兼容和兜底配置
- 启动时不会把 `.env` 反向写回 `config.ini`

### 2. AI 模型配置

Web 端支持：

- 保存多个模型配置
- 应用指定模型配置到运行时
- 测试连接
- 显式声明模型能力
  - `supports_tools`
  - `supports_reasoning`
  - `supports_stream`

### 3. 工作区

工作区分为两种：

- `private`
  当前会话私有
- `shared`
  全局共享

共享工作区默认位于：

```text
data/workspaces/_shared
```

---

## Web 后台功能一览

进入 Web 后台后，可以直接使用这些模块：

- 仪表盘
- 聊天
- 会话管理
- AI 配置
- 记忆管理
- 知识库
- Skills 配置
- Tools 配置
- 工作流
- 定时任务
- Token 用量
- 系统日志
- 调试台

最近版本里，Web 会话侧还加入了：

- 流式回复显示优化
- `/命令` 候选面板
- 文件变更卡片与差异预览
- 移动端竖屏聊天输入区适配

---

## 常用命令示例

项目的 QQ 命令较多，完整命令建议直接查看：

- [docs/docs/guide/commands.md](./docs/docs/guide/commands.md)

常见示例：

```text
/help
/workspace
/ws_send <文件名>
/summary_recent
/summary_today
/findbook <书名>
/jm <漫画ID>
```

---

## 文档入口

仓库内还保留了更细的文档：

- [docs/README.md](./docs/README.md)
- [docs/Chinese.md](./docs/Chinese.md)
- [docs/CHANGELOG.md](./docs/CHANGELOG.md)
- [docs/CONTRIBUTING.md](./docs/CONTRIBUTING.md)
- [docs/docs/guide/quick-start.md](./docs/docs/guide/quick-start.md)

如果你只想快速上手，优先看本 README 和 `quick-start`。

---

## 开发建议

如果你要继续扩展这个项目，建议优先沿着当前架构走：

- 新频道接入放到 `nbot/channels/`
- 新的统一能力放到 `nbot/core/`
- QQ / Web 特有逻辑尽量留在适配层
- 工具、AI 服务和工作区能力放在 `nbot/services/`

这样可以保持“统一 AI 内核，多频道适配”的结构不再回退。

---

## 安全提示

- 不要直接把 Web 后台暴露到公网
- 请为 `WEB_PASSWORD` 设置强密码
- 不要把 API Key、NapCat Token、管理员 QQ 号提交到仓库
- 对共享工作区中的写文件能力保持谨慎

---

## License

本项目使用 MIT License。

相关文件见：

- [LICENSE](./LICENSE)

---

## 致谢

- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)
- [ncatbot](https://github.com/NapNeko/NcatBot)

<div align="center">

Made with care by [AsukaNeko](https://github.com/asukaneko)

</div>
