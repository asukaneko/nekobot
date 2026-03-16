# bot.py 文件文档说明

## 文件概述
bot.py 是Ncatbot-comic-QQbot项目的核心文件，主要负责处理QQ机器人的群聊和私聊消息。文件包含以下主要功能：

1. 消息处理：处理文本、图片、视频、表情、转发消息等多种消息类型
2. 命令响应：处理用户发送的各种命令
3. 特殊功能：包括二次元人物识别、B站视频链接处理等

## 主要功能模块

### 1. 初始化部分
```python
from ncatbot.utils.logger import get_log
import commands
from chat import chat,tts,chat_video,chat_image,chat_webpage,chat_json
from commands import *

_log = get_log()
if_tts = commands.if_tts
emotions = {}
```
- 导入必要的模块和函数
- 初始化日志记录器
- 加载表情数据

### 2. 辅助函数

`load_emotions()`
加载表情数据文件emotions.json

`get_bilibili_real_url(short_url)`
获取B站视频的真实URL

`get_bilibili_video_url(url)`
获取B站视频的播放URL

`deal_forward(msg_obj)`
处理转发消息，解析转发内容并生成摘要

`recognize_image(iurl)`
识别二次元人物，返回人物名称和出处

### 3. 群消息处理
```python
@bot.group_event() 
async def on_group_message(msg: GroupMessage)
``` 

处理群消息的主要逻辑：
1. 处理各种命令
```python
for command, handler in command_handlers.items():
    if isinstance(command, tuple):  # 处理命令别名情况
        for cmd in command:
            if re.match(rf'^{re.escape(cmd)}(?:\s|\.|$)', msg.raw_message):
                await handler(msg, is_group=True)
                _log.info(f"调用{cmd}命令")
                return
    elif re.match(rf'^{re.escape(cmd)}(?:\s|\.|$)', msg.raw_message): # 处理单个命令情况
        await handler(msg, is_group=False)
        _log.info(f"调用{command}命令")
        return
```
2. 处理@机器人的消息
```python
if msg.message[0].get("type") == "at" and msg.message[0].get("data").get("qq") == bot_id:
```
3. 处理回复机器人的消息
```python
if msg.message[0].get("type") == "reply" and msg.message[1].get("type") == "at" and msg.message[1].get("data").get("qq") == bot_id:
```
4. 处理图片、视频、小程序等特殊消息

### 4. 私聊消息处理
```python
@bot.private_event() 
async def on_private_message(msg: PrivateMessage)
```

处理私聊消息的主要逻辑：
1. 处理各种命令
2. 处理小程序消息
3. 处理视频消息
4. 处理图片消息
5. 处理回复消息
6. 处理转发消息
```python
if msg.message[0].get("type") == "forward": # 处理转发消息
    msg_obj = await bot.api.get_msg(message_id=msg.message_id)
    print(msg_obj)
    res = deal_forward(msg_obj)
    content = chat(res, user_id=msg.user_id)
    if if_tts:
        rtf = tts(content)
        await bot.api.set_input_status(event_type=0,user_id=msg.user_id)
        await bot.api.post_private_msg(msg.user_id, rtf=rtf)
    else:
        await bot.api.set_input_status(event_type=1,user_id=msg.user_id)
    await bot.api.post_private_msg(msg.user_id, text=content)
    return
```
7. 处理表情消息

## 使用说明

1. 机器人会自动响应群聊和私聊中的命令
2. 支持@机器人进行对话
3. 支持识别二次元人物（回复图片并发送'/识别人物'）
4. 支持处理B站视频链接
5. 支持语音回复（需配置TTS功能）

        