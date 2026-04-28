# dispatcher - 调度器

## 概述

`dispatcher.py` 提供技能调度功能，负责将用户请求路由到对应的技能处理器。

## SkillDispatcher

```python
from nbot.plugins.dispatcher import SkillDispatcher

dispatcher = SkillDispatcher()
```

## 主要方法

### 注册技能

```python
# 注册技能类
dispatcher.register_skill(MySkill)

# 注册技能实例
skill = MySkill()
dispatcher.register_skill_instance(skill)
```

### 分发请求

```python
async def dispatch(self, request: ChatRequest) -> Optional[ChatResponse]:
    """将请求分发给合适的技能"""
    # 分析请求内容
    # 匹配最佳技能
    # 执行技能
    # 返回结果
```

### 使用示例

```python
from nbot.plugins.dispatcher import SkillDispatcher
from nbot.core.chat_models import ChatRequest

dispatcher = SkillDispatcher()

# 注册技能
dispatcher.register_skill(WeatherSkill)
dispatcher.register_skill(SearchSkill)

# 处理请求
request = ChatRequest(
    channel="web",
    content="北京天气怎么样",
    user_id="user_123"
)

response = await dispatcher.dispatch(request)
if response:
    print(response.final_content)
```

## 路由策略

### 关键词匹配

```python
def _match_by_keywords(self, content: str) -> Optional[Skill]:
    """根据关键词匹配技能"""
    keywords_map = {
        "天气": "weather",
        "搜索": "search",
        "新闻": "news"
    }
    for keyword, skill_name in keywords_map.items():
        if keyword in content:
            return self.get_skill(skill_name)
    return None
```

### 意图识别

```python
async def _match_by_intent(self, content: str) -> Optional[Skill]:
    """使用 AI 进行意图识别"""
    intent = await self.ai_client.classify_intent(content)
    return self.get_skill(intent)
```

## 优先级处理

```python
class SkillPriority:
    HIGH = 1      # 高优先级（精确匹配）
    NORMAL = 2    # 普通优先级（关键词匹配）
    LOW = 3       # 低优先级（意图识别）
    FALLBACK = 4  # 兜底（通用处理）
```

## 执行流程

```
1. 接收 ChatRequest
2. 尝试精确匹配技能
3. 尝试关键词匹配
4. 使用 AI 进行意图识别
5. 执行匹配的技能
6. 如果没有匹配，返回 None（交给 AI 处理）
```

## 与 AI 的协作

```python
# 调度器先尝试技能处理
response = await dispatcher.dispatch(request)

if response:
    # 技能处理了请求
    return response
else:
    # 没有技能匹配，交给 AI 处理
    return await agent_service.process(request)
```
