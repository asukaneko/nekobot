# ai - AI客户端

## 概述

`ai.py` 提供 AI 模型的统一客户端，支持多种提供商（OpenAI、Anthropic、Google 等），处理聊天完成、图片识别、视频分析等功能。

## AIClient 类

```python
from nbot.services.ai import ai_client, AIClient

# 使用全局客户端
response = ai_client.chat_completion(messages)

# 创建新客户端
client = AIClient(
    api_key="your-api-key",
    base_url="https://api.openai.com",
    model="gpt-4",
    provider_type="openai"
)
```

## 主要方法

### 聊天完成

```python
messages = [
    {"role": "system", "content": "你是助手"},
    {"role": "user", "content": "你好"}
]

# 非流式
response = ai_client.chat_completion(messages, stream=False)
print(response.choices[0].message.content)

# 流式
for chunk in ai_client.chat_completion(messages, stream=True):
    print(chunk, end="")
```

### 图片识别

```python
# 识别图片
result = ai_client.describe_image(
    image_url="https://example.com/image.jpg",
    text="描述这张图片"
)
print(result)

# 识别 GIF
result = ai_client.describe_gif("https://example.com/animation.gif")
```

### 视频分析

```python
result = ai_client.describe_video(
    video_url="https://example.com/video.mp4",
    text="分析这个视频的内容"
)
print(result)
```

### 网页分析

```python
result = ai_client.describe_webpage_html(html_content)
print(result)
```

### 搜索功能

```python
# 判断是否需要搜索
needs_search = ai_client.should_search("今天的天气怎么样")

# 执行搜索
search_results = ai_client.search("Python 教程")
```

### 文本摘要

```python
summary = ai_client.summarize_text(
    system_prompt="总结以下内容",
    user_prompt=long_text
)
```

## 运行时配置

支持从 Web 配置动态加载模型设置：

```python
from nbot.services.ai import refresh_runtime_ai_config

# 刷新配置
config = refresh_runtime_ai_config()
print(f"当前模型: {config['model']}")
print(f"提供商: {config['provider_type']}")
```

## 多模型支持

支持配置多个专用模型：

```python
# 图片理解模型
vision_config = get_vision_model_config()

# 视频理解模型
video_config = get_video_model_config()

# TTS 模型
tts_config = get_tts_model_config()

# STT 模型
stt_config = get_stt_model_config()

# 嵌入模型
embedding_config = get_embedding_model_config()
```

## 提供商支持

| 提供商 | 标识 | 特点 |
|--------|------|------|
| OpenAI | `openai` | 原生支持，功能完整 |
| Anthropic | `anthropic` | Claude 系列，推理能力强 |
| Google | `google` | Gemini 系列，多模态 |
| MiniMax | `minimax` | 中文优化 |
| SiliconFlow | `siliconflow` | 国内服务 |

## 环境变量

```bash
# 主 API Key
API_KEY=your-api-key

# 特定提供商
OPENAI_API_KEY=sk-xxx
ANTHROPIC_API_KEY=sk-ant-xxx
GEMINI_API_KEY=xxx
MINIMAX_API_KEY=xxx
SILICON_API_KEY=xxx
```
