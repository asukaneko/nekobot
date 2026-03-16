# 快速开始

> 一个可以下载本子和 AI 聊天的 QQ 猫娘机器人

## 环境要求

::: warning
- Python 3.11+
- Windows 环境
- 建议使用小号登录
:::

::: info
基于 [jmcomic](https://github.com/hect0x7/JMComic-Crawler-Python) 和 [ncatbot](https://github.com/liyihao1110/ncatbot) 开发
:::

## 安装

### 1. 克隆项目

```bash
git clone https://github.com/asukaneko/Ncatbot-comic-QQbot.git
cd Ncatbot-comic-QQbot
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置 NapCat

首次运行会自动提示下载 NapCat，或手动下载：

- [NapCat 下载](https://github.com/NapNeko/NapCatQQ/releases)
- 解压到根目录，重命名为 `napcat`

### 4. 配置 bot

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

## 运行

```bash
# 启动机器人
python bot.py

# 启动 Web 界面
python bot.py --web
```

## 项目结构

```
Ncatbot-comic-QQbot/
├── bot.py                    # 入口文件
├── nbot/                     # 核心模块
│   ├── commands.py          # 命令处理
│   ├── chat.py             # 聊天兼容层
│   ├── config.py           # 配置兼容层
│   ├── heartbeat.py         # 心跳模块
│   ├── core/               # 核心功能
│   │   ├── memory.py       # 记忆系统
│   │   ├── knowledge.py    # 知识库
│   │   └── workflow.py     # 工作流
│   ├── plugins/            # 插件系统
│   └── services/           # 服务层
│
├── resources/               # 静态资源
│   ├── config/             # 配置文件
│   │   ├── option.yml
│   │   ├── urls.ini
│   │   └── novel_details2.json
│   └── prompts/            # 提示词
│       ├── neko.txt        # 默认提示词
│       ├── user/           # 用户提示词
│       └── group/          # 群组提示词
│
├── docs/                    # 详细文档
└── tools/                   # 工具脚本
```

## 配置文件说明

| 文件 | 说明 |
|------|------|
| `config.ini` | 账号、API、设置 |
| `resources/config/option.yml` | 漫画下载配置 |
| `resources/config/urls.ini` | 图片 API |
| `resources/prompts/neko.txt` | AI 角色提示词 |

## 下一步

- [命令手册](./commands.md) - 了解所有命令
- [开发指南](./guide.md) - 开发自己的功能
- [配置说明](../guide/page/config.md) - 详细配置
