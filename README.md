# 🤖 NekoBot

<div align="center">

![Python](https://img.shields.io/badge/Python-3.11+-blue?style=flat&logo=python)
![License](https://img.shields.io/badge/License-MIT-green?style=flat)
![Version](https://img.shields.io/badge/Version-2.1.0-purple?style=flat)

**一个可以下载本子和 AI 聊天的 QQ 猫娘机器人**

[快速开始](#-快速开始) · [文档](http://nekodocs.s.odn.cc/)

</div>

---

## ✨ 特性

<table>
<tr>
<td valign="top">

### 🎨 核心功能

- 📚 **漫画下载** - 支持 JMComic 漫画下载
- 💬 **AI 聊天** - 基于大模型的智能对话
- 🎵 **多媒体** - 音乐、图片、视频识别
- 📖 **轻小说** - 搜索和下载轻小说
- 🎮 **娱乐功能** - 随机图片、表情包、抽卡
- 📁 **工作区** - 私有和共享文件管理

</td>
<td valign="top">

### 🛠️ 技术特性

- 🔌 **插件系统** - 灵活的技能扩展
- 🧠 **记忆系统** - 用户个性化记忆
- 📚 **知识库** - RAG 智能问答
- ⚡ **工作流** - 自动化任务编排
- 🌐 **Web 界面** - 在线聊天和配置
- 🔄 **私有/共享工作区** - AI 工具区分文件来源

</td>
</tr>
</table>

---

## 🚀 快速开始

### 1️⃣ 克隆项目

```bash
git clone https://github.com/asukaneko/Ncatbot-comic-QQbot.git
cd Ncatbot-comic-QQbot
```

### 2️⃣ 安装依赖

```bash
pip install -r requirements.txt
```

### 3️⃣ 配置

编辑 `config.ini`：

```ini
[BotConfig]
bot_uin = 你的QQ号
ws_uri = ws://localhost:3001
root = 管理员QQ号

[ApiKey]
api_key = 你的API密钥
base_url = https://api.minimaxi.com/v1
model = M2-her
```

### 4️⃣ 运行

```bash
# 启动机器人
python bot.py

# 或启动 Web 界面
python bot.py --web
```

---

## 📁 工作区系统

项目包含完整的文件管理工作区系统：

### 私有工作区
- 每个会话独立的文件存储空间
- AI 工具可创建、读取、编辑、删除文件
- 支持拖拽移动文件

### 共享工作区
- 所有会话共享的工作区 (`data/workspaces/_shared`)
- AI 工具通过 `scope` 参数区分
- 适合存放通用配置文件、素材等

### AI 工具使用

```python
# 列出文件（支持递归）
workspace_list_files(scope="all", recursive=true)

# 读取文件（区分来源）
workspace_read_file(filename="config.json", scope="shared")

# 创建文件
workspace_create_file(filename="notes.txt", content="...", scope="private")
```

---

## 📖 文档

| 文档 | 说明 |
|------|------|
| [快速开始](https://docs.ncatbot.xyz/guide/quick-start) | 完整的安装配置指南 |
| [命令列表](https://docs.ncatbot.xyz/guide/commands) | 所有可用命令 |
| [API 文档](https://docs.ncatbot.xyz/napcat/api) | NapCat 接口 |
| [开发指南](https://docs.ncatbot.xyz/guide/dev) | 开发文档 |

---

## 📁 项目结构

```
Ncatbot-comic-QQbot/
├── bot.py                    # 入口文件
│
├── nbot/                     # 核心模块
│   ├── commands.py          # 命令处理
│   ├── chat.py              # 聊天服务
│   ├── config.py            # 配置加载
│   ├── web/                 # Web 界面
│   ├── core/                # 核心功能
│   │   ├── heartbeat.py     # 心跳
│   │   ├── memory.py        # 记忆系统
│   │   ├── knowledge.py      # 知识库
│   │   ├── workflow.py      # 工作流
│   │   └── workspace.py      # 工作区管理
│   ├── plugins/             # 插件系统
│   └── services/            # 服务层
│       └── tools.py         # AI 工具
│
├── data/                    # 数据目录
│   ├── sessions/           # 会话数据
│   ├── memories/           # 记忆数据
│   ├── workspaces/        # 工作区
│   │   └── _shared/       # 共享工作区
│   └── web/               # Web 数据
│
├── resources/               # 静态资源
│   ├── config/            # 配置文件
│   └── prompts/           # 提示词
│
└── tools/                  # 工具脚本
```

---

## ⌨️ 常用命令

| 命令 | 说明 |
|------|------|
| `/jm <ID>` | 下载漫画 |
| `/search <关键词>` | 搜索漫画 |
| `/chat` 或 `@机器人` | AI 对话 |
| `/gf <描述>` | AI 生成图片 |
| `/help` | 查看帮助 |

---

## 💡 提示

- 登录后在 `napcat/logs` 文件夹可找到 WebUI 登录地址
- 可修改 `resources/prompts/neko.txt` 定制角色
- 工作区工具支持 `scope` 参数区分私有/共享文件
- 详细功能说明请查看 [docs](./docs/) 目录

---

## 📄 许可证

MIT License - 查看 [LICENSE](./docs/CODE_OF_CONDUCT.md)

---

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

查看 [贡献指南](./docs/CONTRIBUTING.md)

---

<div align="center">

Made with ❤️ by [AsukaNeko](https://github.com/asukaneko)

</div>
