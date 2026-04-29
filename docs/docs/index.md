---
layout: home

hero:
  name: "NekoBot"
  text: "多频道 AI 机器人"
  tagline: 一个面向 QQ 与 Web 双频道的 AI 机器人，集成聊天、工作区、工具调用、知识库、记忆、工作流与可视化管理后台
  image:
    src: /neko.png
    alt: NekoBot
  actions:
    - theme: brand
      text: 🚀 快速开始
      link: /guide/quick-start.md
    - theme: alt
      text: 📖 命令文档
      link: /guide/commands.md
    - theme: alt
      text: 💻 开发指南
      link: /guide/guide.md

features:
  - icon: 🌐
    title: 多频道支持
    details: 支持 QQ、Web、Telegram 等多频道接入，统一 AI 内核，频道适配层分离
  - icon: 💬
    title: AI 聊天
    details: 基于大模型的智能对话，支持工具调用、多轮对话、上下文管理、流式回复
  - icon: 🧠
    title: 记忆系统
    details: 用户个性化记忆，知识库 RAG 智能问答，支持私有和共享工作区
  - icon: 🔧
    title: 工具调用
    details: 支持文件操作、搜索、代码执行等工具调用，可继续执行中断任务
  - icon: 🔄
    title: 工作流
    details: 可视化工作流编排，支持定时任务和自动化流程
  - icon: 🎛️
    title: Web 后台
    details: 完整的 Web 管理界面，支持会话管理、AI 配置、模型管理、日志监控
---

<div align="center">

**[功能演示](https://github.com/asukaneko/nekobot)** · **[更新日志](./guide/changelog.md)** · **[贡献指南](https://github.com/asukaneko/nekobot/blob/main/docs/CONTRIBUTING.md)**

</div>

## 项目简介

NekoBot 是一个面向 QQ 与 Web 双频道的 AI 机器人项目，采用"统一 AI 内核 + 频道适配层"的架构设计：

- **QQ 与 Web 共用同一套 AI 核心** - 通过统一的 `ChatRequest` / `ChatResponse` 处理链路
- **支持会话级与共享工作区** - 文件创建、修改、删除、发送，支持 diff 预览
- **支持工具调用与多轮对话** - 可继续执行中断的工具调用任务
- **提供 Web 仪表盘** - AI 配置中心、工作流、日志与调试界面
- **支持多模型配置** - 保存多个模型配置，运行时切换

## 架构概览

```
Ncatbot-comic-QQbot/
├── bot.py                 # 入口文件，启动 QQ 服务与 Web 服务
├── nbot/                  # 核心模块包
│   ├── channels/          # 频道适配层（QQ / Web / Telegram）
│   ├── core/              # 统一 AI 核心
│   │   ├── agent_service.py   # AI 处理入口
│   │   ├── chat_models.py     # ChatRequest / ChatResponse
│   │   ├── session_store.py   # 会话读写
│   │   └── model_adapter.py   # 模型适配层
│   ├── plugins/           # 插件系统
│   ├── services/          # AI、工具、聊天服务
│   └── web/               # Web 后台与前端
├── data/                  # 运行数据
│   ├── qq/               # QQ 相关数据
│   ├── web/              # Web 会话、模型配置
│   └── workspaces/       # 私有 / 共享工作区
└── resources/            # 静态资源
```

## 快速启动

```bash
# 克隆项目
git clone https://github.com/asukaneko/nekobot.git
cd nekobot

# 安装依赖
pip install -r requirements.txt

# 配置环境变量（复制 .env.example 为 .env 并编辑）
cp .env.example .env

# 启动（QQ + Web）
python bot.py

# 仅启动 Web
python bot.py --only-web

# 仅启动 QQ
python bot.py --no-web

# CLI + Web 模式
python bot.py --cli-and-web
```

## 技术栈

- **Python 3.11+** - 主要开发语言
- **NapCat + ncatbot** - QQ 协议接入
- **Flask + Socket.IO** - Web 服务
- **Vue.js** - 前端界面
- **RAG** - 知识库检索
