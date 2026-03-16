# chat.py 文档

## 文件概述

chat.py 是一个QQ机器人聊天模块，主要功能包括：
- 与AI进行对话
- 处理图片、视频、网页内容识别
- 语音合成(TTS)
- 管理用户和群组的聊天记录

## 主要功能

### 1. 聊天功能 (`chat` 函数)
```python
chat(content="", user_id=None, group_id=None, group_user_id=None, image=False, url=None, video=None)
```
参数说明：
- `content`: 用户输入内容
- `user_id`: 用户ID（私聊时使用）
- `group_id`: 群组ID（群聊时使用）
- `group_user_id`: 群组中的用户ID
- `image`: 是否为图片消息
- `url`: 图片URL
- `video`: 视频URL

### 2. 多媒体处理
- `chat_image()`: 图片识别
- `chat_video()`: 视频识别
- `chat_webpage()`: 网页内容识别
- `chat_json()`: JSON内容分析

### 3. 语音功能
- `tts()`: 文本转语音
- `upload_voice()`: 上传音频文件

## 配置文件依赖

从config.ini读取以下配置：
- API密钥和基础URL
- 聊天模型设置
- 最大历史记录长度
- 图片识别模型
- 缓存地址

## 数据存储

- 用户聊天记录保存在`saved_message/user_messages.json`
- 群组聊天记录保存在`saved_message/group_messages.json`
- 提示词保存在`prompts/`目录下

## 使用示例

```python
# 私聊
response = chat(content="你好", user_id=123456)

# 群聊
response = chat(content="大家好", group_id=789012, group_user_id=123456)

# 图片识别
response = chat(content="描述这张图片", image=True, url="https://example.com/image.jpg")

# 语音合成
tts_message = tts("你好，我是机器人")
```
        