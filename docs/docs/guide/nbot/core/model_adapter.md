# model_adapter - 模型适配

## 概述

`model_adapter.py` 提供对不同 AI 模型提供商的统一适配，支持 OpenAI、Anthropic、Google 等多种 API 格式。

## 支持的提供商

| 提供商 | 标识 | 说明 |
|--------|------|------|
| OpenAI | `openai` | 原生 OpenAI API |
| Anthropic | `anthropic` | Claude API |
| Google | `google` / `gemini` | Gemini API |
| MiniMax | `minimax` | MiniMax API |
| SiliconFlow | `siliconflow` | 硅基流动 API |
| 自定义 | `openai_compatible` | 兼容 OpenAI 格式的自定义端点 |

## 核心函数

### build_chat_completion_payload()

构建聊天完成的请求体。

```python
from nbot.core.model_adapter import build_chat_completion_payload

payload = build_chat_completion_payload(
    model="gpt-4",
    messages=[
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "你好"}
    ],
    base_url="https://api.openai.com",
    provider_type="openai",
    stream=True
)
```

### normalize_chat_completion_data()

标准化不同提供商的响应格式。

```python
from nbot.core.model_adapter import normalize_chat_completion_data

# 无论原始响应是什么格式，都转换为统一格式
normalized = normalize_chat_completion_data(
    raw_response,
    base_url="https://api.openai.com",
    model="gpt-4",
    provider_type="openai"
)

print(normalized.content)      # 提取的内容
print(normalized.usage)        # Token 使用情况
```

### resolve_chat_completion_url()

解析正确的 API 端点 URL。

```python
from nbot.core.model_adapter import resolve_chat_completion_url

url = resolve_chat_completion_url(
    base_url="https://api.openai.com",
    model="gpt-4",
    provider_type="openai"
)
# 返回: https://api.openai.com/v1/chat/completions
```

## 提供商特定适配

### Anthropic 适配

```python
from nbot.services.anthropic_adapter import (
    build_anthropic_payload,
    parse_anthropic_response,
    get_anthropic_headers,
)

# Anthropic 使用不同的消息格式和端点
headers = get_anthropic_headers(api_key)
payload = build_anthropic_payload(
    model="claude-3-opus",
    messages=messages,
    max_tokens=4096
)
```

### Google Gemini 适配

```python
# Gemini 使用不同的认证和端点格式
url = f"https://generativelanguage.googleapis.com/v1/models/{model}:generateContent"
headers = {"x-goog-api-key": api_key}
```

## 能力声明

每个模型配置可以声明支持的能力：

```python
model_config = {
    "model": "gpt-4",
    "provider_type": "openai",
    "supports_tools": True,       # 支持工具调用
    "supports_reasoning": True,   # 支持推理过程
    "supports_stream": True       # 支持流式输出
}
```

## 运行时配置切换

支持在运行时动态切换模型配置：

```python
from nbot.services.ai import refresh_runtime_ai_config

# 从 Web 配置加载并应用新的模型设置
config = refresh_runtime_ai_config()
print(f"当前模型: {config['model']}")
```

## 多模型配置

支持配置多个模型并在运行时切换：

```python
# data/web/ai_models.json
{
    "active_model_id": "model_1",
    "models": [
        {
            "id": "model_1",
            "name": "GPT-4",
            "model": "gpt-4",
            "provider_type": "openai",
            "enabled": True
        },
        {
            "id": "model_2",
            "name": "Claude 3",
            "model": "claude-3-opus",
            "provider_type": "anthropic",
            "enabled": True
        }
    ]
}
```
