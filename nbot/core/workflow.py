"""
Workflow 自动化工作流系统
支持定时任务和事件触发的工作流
"""
import os
import json
import yaml
import logging
import asyncio
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
import re

_log = logging.getLogger(__name__)


class TriggerType(Enum):
    """触发类型"""
    CRON = "cron"           # 定时触发
    EVENT = "event"         # 事件触发
    MANUAL = "manual"       # 手动触发
    WEBHOOK = "webhook"     # Webhook触发


class WorkflowStatus(Enum):
    """工作流状态"""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class WorkflowStep:
    """工作流步骤"""
    action: str
    input: Dict[str, Any] = field(default_factory=dict)
    output_var: str = None
    condition: str = None
    retry: int = 0
    timeout: int = 60


@dataclass
class Workflow:
    """工作流定义"""
    name: str
    description: str = ""
    trigger: Dict[str, Any] = field(default_factory=dict)
    steps: List[WorkflowStep] = field(default_factory=list)
    enabled: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Workflow":
        steps = [WorkflowStep(**step) for step in data.get("steps", [])]
        return cls(
            name=data["name"],
            description=data.get("description", ""),
            trigger=data.get("trigger", {}),
            steps=steps,
            enabled=data.get("enabled", True),
            metadata=data.get("metadata", {})
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "steps": [vars(step) for step in self.steps],
            "enabled": self.enabled,
            "metadata": self.metadata
        }


@dataclass
class WorkflowInstance:
    """工作流实例"""
    workflow_name: str
    status: str = WorkflowStatus.IDLE.value
    current_step: int = 0
    variables: Dict[str, Any] = field(default_factory=dict)
    logs: List[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now().isoformat())
    finished_at: str = None
    error: str = None


class WorkflowEngine:
    """工作流引擎"""

    def __init__(self, workflows_dir: str = "workflows"):
        self.workflows_dir = Path(workflows_dir)
        self.workflows: Dict[str, Workflow] = {}
        self.instances: Dict[str, WorkflowInstance] = {}
        self._action_handlers: Dict[str, Callable] = {}
        self._cron_tasks: Dict[str, asyncio.Task] = {}

        self.workflows_dir.mkdir(exist_ok=True)
        self._register_default_actions()

    def _register_default_actions(self):
        """注册默认动作"""
        self.register_action("log", self._action_log)
        self.register_action("send_message", self._action_send_message)
        self.register_action("fetch_data", self._action_fetch_data)
        self.register_action("condition", self._action_condition)

    def register_action(self, name: str, handler: Callable):
        """注册动作处理器"""
        self._action_handlers[name] = handler
        _log.info(f"Registered action handler: {name}")

    async def _action_log(self, step: WorkflowStep, instance: WorkflowInstance) -> bool:
        """日志动作"""
        message = step.input.get("message", "")
        instance.logs.append(f"[LOG] {message}")
        return True

    async def _action_send_message(self, step: WorkflowStep, instance: WorkflowInstance) -> bool:
        """发送消息动作"""
        message = step.input.get("message", "")
        target = step.input.get("target", "all")

        instance.logs.append(f"[SEND] To {target}: {message}")
        return True

    async def _action_fetch_data(self, step: WorkflowStep, instance: WorkflowInstance) -> bool:
        """获取数据动作"""
        url = step.input.get("url", "")
        instance.logs.append(f"[FETCH] {url}")
        return True

    async def _action_condition(self, step: WorkflowStep, instance: WorkflowInstance) -> bool:
        """条件判断动作"""
        condition = step.input.get("condition", "")
        instance.logs.append(f"[CONDITION] {condition}")
        return True

    def load_workflows(self):
        """加载工作流"""
        if not self.workflows_dir.exists():
            return

        for file in self.workflows_dir.glob("*.yaml"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f)
                    workflow = Workflow.from_dict(data)
                    self.workflows[workflow.name] = workflow
                    _log.info(f"Loaded workflow: {workflow.name}")
            except Exception as e:
                _log.error(f"Failed to load workflow {file}: {e}")

    def save_workflow(self, workflow: Workflow):
        """保存工作流"""
        file_path = self.workflows_dir / f"{workflow.name}.yaml"
        with open(file_path, "w", encoding="utf-8") as f:
            yaml.dump(workflow.to_dict(), f, allow_unicode=True)

    async def execute_workflow(
        self,
        workflow_name: str,
        context: Dict[str, Any] = None
    ) -> WorkflowInstance:
        """执行工作流"""
        workflow = self.workflows.get(workflow_name)
        if not workflow:
            raise ValueError(f"Workflow not found: {workflow_name}")

        if not workflow.enabled:
            raise ValueError(f"Workflow is disabled: {workflow_name}")

        instance = WorkflowInstance(workflow_name=workflow_name)
        instance.variables = context or {}
        self.instances[workflow_name] = instance

        instance.status = WorkflowStatus.RUNNING.value

        try:
            for i, step in enumerate(workflow.steps):
                instance.current_step = i
                instance.logs.append(f"[STEP {i+1}] Executing: {step.action}")

                action_handler = self._action_handlers.get(step.action)
                if not action_handler:
                    instance.logs.append(f"[ERROR] Unknown action: {step.action}")
                    instance.status = WorkflowStatus.ERROR.value
                    break

                success = await action_handler(step, instance)

                if not success:
                    instance.logs.append(f"[ERROR] Step failed: {step.action}")
                    instance.status = WorkflowStatus.ERROR.value
                    break

                if step.output_var:
                    instance.variables[step.output_var] = "success"

            if instance.status == WorkflowStatus.RUNNING.value:
                instance.status = WorkflowStatus.IDLE.value

        except Exception as e:
            instance.status = WorkflowStatus.ERROR.value
            instance.error = str(e)
            instance.logs.append(f"[ERROR] {e}")

        instance.finished_at = datetime.now().isoformat()
        return instance

    def get_workflow(self, name: str) -> Optional[Workflow]:
        """获取工作流"""
        return self.workflows.get(name)

    def list_workflows(self) -> List[Dict[str, Any]]:
        """列出所有工作流"""
        return [
            {
                "name": wf.name,
                "description": wf.description,
                "trigger": wf.trigger,
                "enabled": wf.enabled,
                "steps_count": len(wf.steps)
            }
            for wf in self.workflows.values()
        ]

    def enable_workflow(self, name: str) -> bool:
        """启用工作流"""
        if name in self.workflows:
            self.workflows[name].enabled = True
            self.save_workflow(self.workflows[name])
            return True
        return False

    def disable_workflow(self, name: str) -> bool:
        """禁用工作流"""
        if name in self.workflows:
            self.workflows[name].enabled = False
            self.save_workflow(self.workflows[name])
            return True
        return False


def create_sample_workflow(name: str = "daily_comic_push") -> Workflow:
    """创建示例工作流"""
    if name == "daily_comic_push":
        return Workflow(
            name="daily_comic_push",
            description="每日漫画推送",
            trigger={
                "type": "cron",
                "expression": "0 9 * * *"
            },
            steps=[
                WorkflowStep(
                    action="fetch_data",
                    input={"url": "https://api.example.com/latest-comics"},
                    output_var="comics"
                ),
                WorkflowStep(
                    action="condition",
                    input={"condition": "{{comics}}"}
                ),
                WorkflowStep(
                    action="send_message",
                    input={
                        "message": "今日漫画更新：{{comics}}",
                        "target": "subscribed_groups"
                    }
                ),
                WorkflowStep(
                    action="log",
                    input={"message": "Daily push completed"}
                )
            ]
        )


workflow_engine: Optional[WorkflowEngine] = None


def get_workflow_engine() -> WorkflowEngine:
    """获取工作流引擎单例"""
    global workflow_engine
    if workflow_engine is None:
        workflow_engine = WorkflowEngine()
        workflow_engine.load_workflows()
    return workflow_engine
