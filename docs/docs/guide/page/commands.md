# commands.py 文件文档

## 概述
`commands.py`是QQ机器人Ncatbot-comic-QQbot的主要命令处理文件，包含了机器人的核心功能实现。

## 主要功能

### 1. 全局变量
- `bot_id`, `admin_id`: 机器人QQ号和管理员ID
- `command_handlers`: 命令处理器字典
- `user_favorites`: 用户收藏夹
- `group_favorites`: 群组收藏夹
- `black_list_comic`: 漫画黑名单
- `running`: 定时聊天开关
- `tasks`: 定时任务存储
- `at_all_group`: @全体成员群列表

### 2. 通用函数
- 文件读写操作: `write_at_all_group()`, `load_admin()`等
- 定时任务: `schedule_task()`, `schedule_job_task()`
- 聊天功能: `chatter()`, `chat_loop()`
- 数据加载: `load_favorites()`, `load_novel_data()`

### 3. 命令注册
使用 `@register_command` 装饰器注册命令，例如:
```python
def register_command(*command,help_text = None,admin_show = False,category = "1"): # 注册命令
    """
    装饰器，用于注册命令。
    :param command: 命令名称，支持多个。
    :param help_text: 命令的帮助文本。
    :param admin_show: 是否在管理员帮助中显示，默认False。
    :param category: 命令的类别，默认"1"。
    """
    def decorator(func):
        command_handlers[command] = func
        func.help_text = help_text
        func.admin_show = admin_show
        func.category = category
        return func
    return decorator

@register_command("测试")
async def handle_test(msg, is_group=True):
    # 测试命令实现
```

### 4. 主要命令类别

#### 漫画相关命令
- `/jmrank`: 获取漫画排行榜
- `/search`: 搜索漫画
- `/tag`: 按标签搜索漫画
- `/get_fav`: 获取收藏夹
- `/jm`: 下载漫画

#### 系统命令
- `/tts`: 开启/关闭TTS
- 测试命令

## 文件结构
文件使用region注释划分不同功能区域，便于维护:
```python
#----------------------
# region 全局变量设置
#----------------------

#----------------------
#     region 命令
#----------------------
```

## 数据存储
- 使用JSON文件存储收藏夹、黑名单等数据
- 使用YAML配置文件
- 使用文本文件存储管理员列表等

## 注意事项
- 文件包含大量异步函数(async/await)
- 使用第三方库jmcomic处理漫画相关功能
- 包含详细的错误处理和日志输出
        