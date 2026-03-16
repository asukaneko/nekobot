# config.py 文件文档

## 概述
`config.py` 是QQ机器人Ncatbot-comic-QQbot的配置文件处理模块，主要负责读取和设置机器人的配置信息。

## 主要功能

### 1. 配置加载
通过 `load_config()` 函数加载 `config.ini` 文件中的配置信息：
- `bot_uin`: 机器人QQ号
- `root`: 根目录路径
- `ws_uri`: WebSocket连接地址(默认: ws://localhost:3001)
- `token`: 认证令牌

### 2. 配置设置
使用 `ncatbot.utils.config` 模块的 `config` 对象设置配置：
```python
config.set_bot_uin(bot_uin)
config.set_root(root)
config.set_webui_uri("xxxxxx:6099")  # 可选: 自定义webui地址
```

### 3. 注意事项
- 如果使用远程连接，需要确保本地下载的文件能被napcat服务器访问
- 配置文件路径为 `config.ini`
- 返回机器人QQ号和根目录路径

## 代码结构
```python
import configparser
from ncatbot.utils.config import config

def load_config():
    # 读取config.ini文件
    # 设置配置项
    # 返回bot_uin和root
```

## 相关文件
- `config.ini`: 主配置文件
- `ncatbot/utils/config.py`: 配置工具模块
        