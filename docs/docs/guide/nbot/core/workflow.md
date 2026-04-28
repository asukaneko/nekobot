# workflow - 工作流

## 概述

`workflow.py` 提供可视化工作流编排功能，支持创建自动化流程、定时任务和复杂的多步骤操作。

## 核心概念

### 工作流 (Workflow)

由多个节点组成的有向图，定义了自动化的执行流程。

```python
@dataclass
class Workflow:
    id: str                           # 工作流ID
    name: str                         # 名称
    description: str                  # 描述
    nodes: List[Node]                 # 节点列表
    edges: List[Edge]                 # 连接边
    triggers: List[Trigger]           # 触发器
    enabled: bool                     # 是否启用
```

### 节点 (Node)

工作流的基本执行单元。

```python
@dataclass
class Node:
    id: str                           # 节点ID
    type: str                         # 节点类型
    config: Dict[str, Any]            # 节点配置
    position: Dict[str, float]        # 位置坐标

# 节点类型
NODE_TYPES = {
    "start": "开始节点",
    "end": "结束节点",
    "ai": "AI 调用",
    "tool": "工具调用",
    "condition": "条件判断",
    "delay": "延迟等待",
    "webhook": "HTTP 请求",
    "message": "发送消息",
    "script": "自定义脚本"
}
```

### 触发器 (Trigger)

触发工作流执行的条件。

```python
@dataclass
class Trigger:
    type: str                         # 触发器类型
    config: Dict[str, Any]            # 触发器配置

# 触发器类型
TRIGGER_TYPES = {
    "manual": "手动触发",
    "schedule": "定时触发",
    "webhook": "Webhook 触发",
    "event": "事件触发"
}
```

## 工作流引擎

### WorkflowEngine

工作流执行引擎。

```python
class WorkflowEngine:
    def __init__(self):
        self.workflows: Dict[str, Workflow] = {}
        self.executions: Dict[str, Execution] = {}

    async def execute(self, workflow_id: str, context: Dict = None) -> Execution:
        """执行工作流"""
        pass

    async def execute_node(self, node: Node, context: Dict) -> NodeResult:
        """执行单个节点"""
        pass
```

## 使用示例

### 创建工作流

```python
from nbot.core.workflow import Workflow, Node, Edge, WorkflowEngine

# 创建工作流
workflow = Workflow(
    id="wf_001",
    name="天气提醒",
    description="每天早上发送天气信息",
    nodes=[
        Node(id="start", type="start", config={}, position={"x": 100, "y": 100}),
        Node(id="get_weather", type="tool", config={"tool": "get_weather", "city": "北京"}, position={"x": 300, "y": 100}),
        Node(id="send_msg", type="message", config={"content": "今天天气: {get_weather.result}"}, position={"x": 500, "y": 100}),
        Node(id="end", type="end", config={}, position={"x": 700, "y": 100}),
    ],
    edges=[
        Edge(source="start", target="get_weather"),
        Edge(source="get_weather", target="send_msg"),
        Edge(source="send_msg", target="end"),
    ],
    triggers=[
        Trigger(type="schedule", config={"cron": "0 8 * * *"})
    ]
)

# 注册并执行
engine = WorkflowEngine()
engine.register(workflow)
```

### 执行工作流

```python
# 手动触发
result = await engine.execute("wf_001", context={"user_id": "123"})

# 查看执行结果
print(f"执行状态: {result.status}")
print(f"执行日志: {result.logs}")
```

## 节点详解

### AI 节点

调用 AI 模型处理数据。

```python
{
    "type": "ai",
    "config": {
        "model": "gpt-4",
        "prompt": "分析以下内容: {input.text}",
        "temperature": 0.7
    }
}
```

### 工具节点

调用系统工具。

```python
{
    "type": "tool",
    "config": {
        "tool": "search_web",
        "arguments": {
            "query": "{input.keyword}"
        }
    }
}
```

### 条件节点

根据条件分支执行。

```python
{
    "type": "condition",
    "config": {
        "condition": "{input.score} > 80",
        "true_branch": "node_a",
        "false_branch": "node_b"
    }
}
```

### 延迟节点

等待指定时间。

```python
{
    "type": "delay",
    "config": {
        "duration": 60  # 秒
    }
}
```

## 变量传递

工作流中可以使用 `{node_id.output}` 语法引用其他节点的输出：

```python
# 节点A: 获取天气
{"type": "tool", "id": "weather", "config": {"tool": "get_weather"}}

# 节点B: 使用天气结果发送消息
{"type": "message", "config": {"content": "天气: {weather.result.temperature}°C"}}
```

## Web 界面

工作流可以在 Web 后台的可视化编辑器中创建和编辑：

- 拖拽节点到画布
- 连接节点形成流程
- 配置节点参数
- 设置触发器
- 查看执行历史
