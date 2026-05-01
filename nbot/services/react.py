import asyncio
import logging
from typing import List, Optional
from dataclasses import dataclass, field

from nbot.plugins.skills.base import SkillContext
from nbot.plugins.manager import get_plugin_manager
from nbot.plugins.dispatcher import get_skill_dispatcher

_log = logging.getLogger(__name__)


@dataclass
class ThoughtStep:
    """思考步骤"""
    step: int
    thought: str
    action: Optional[str] = None
    action_input: Optional[str] = None
    observation: Optional[str] = None
    is_final: bool = False


@dataclass
class ReActResult:
    """思考链结果"""
    success: bool
    final_answer: str
    thought_steps: List[ThoughtStep] = field(default_factory=list)
    error: Optional[str] = None


class ReActAgent:
    """
    ReAct (Reasoning + Acting) Agent
    让 AI 能够循环思考和调用工具，直到能回答问题
    """

    def __init__(
        self,
        max_iterations: int = 5,
        timeout: int = 60
    ):
        self.max_iterations = max_iterations
        self.timeout = timeout
        self.plugin_manager = get_plugin_manager()
        self._dispatcher = None

    def _get_dispatcher(self):
        if self._dispatcher is None:
            self._dispatcher = get_skill_dispatcher(self.plugin_manager)
        return self._dispatcher

    def _build_thinking_prompt(
        self,
        question: str,
        context: SkillContext,
        thought_history: List[ThoughtStep]
    ) -> str:
        """构建思考提示词"""

        history_text = ""
        if thought_history:
            history_text = "\n\n### 思考历史\n"
            for step in thought_history:
                history_text += f"\n**步骤 {step.step}**\n"
                if step.thought:
                    history_text += f"思考: {step.thought}\n"
                if step.action:
                    history_text += f"行动: 调用技能 {step.action}\n"
                if step.action_input:
                    history_text += f"输入: {step.action_input}\n"
                if step.observation:
                    history_text += f"观察结果: {step.observation}\n"

        prompt = f"""你是一个智能助手，需要通过思考和调用工具来回答用户的问题。

## 用户问题
{question}

## 可用工具
{self._get_dispatcher().get_available_skills_prompt()}

## 思考格式
请按以下格式进行思考：

思考: <你对问题的分析>
行动: <要调用的技能名称，如果没有则写"无">
输入: <技能需要的参数，如果没有则写"无">
观察结果: <技能返回的结果，用于下一步思考>

注意：
1. 如果当前信息足以回答问题，行动写"完成"，然后直接给出最终答案
2. 如果需要更多信息或需要调用工具，先分析需要什么，然后调用相应技能
3. 每次只调用一个技能，等待结果后再进行下一步思考
4. 如果超过{self.max_iterations}次迭代仍未得到答案，请给出当前最好的答案{history_text}

现在开始你的思考：
"""

        return prompt

    async def think(
        self,
        question: str,
        context: SkillContext,
        ai_client
    ) -> ReActResult:
        """
        执行思考链

        Args:
            question: 用户问题
            context: 技能执行上下文
            ai_client: AI 客户端

        Returns:
            ReActResult: 思考结果
        """
        thought_history: List[ThoughtStep] = []

        for iteration in range(self.max_iterations):
            step_num = iteration + 1
            _log.info(f"[ReAct] Iteration {step_num}/{self.max_iterations}")

            prompt = self._build_thinking_prompt(question, context, thought_history)

            try:
                response = ai_client.chat_completion(
                    messages=[
                        {"role": "system", "content": "你是一个善于思考的AI助手。"},
                        {"role": "user", "content": prompt}
                    ],
                    stream=False
                )

                content = ai_client.clean_response(
                    response.choices[0].message.content
                )

                thought_step = self._parse_thought(content, step_num)
                thought_history.append(thought_step)

                _log.info(f"[ReAct] Step {step_num}: {thought_step.thought[:100]}...")
                if thought_step.action:
                    _log.info(f"[ReAct] Action: {thought_step.action}")

                if thought_step.action == "完成" or thought_step.is_final:
                    final_answer = thought_step.observation or thought_step.thought
                    return ReActResult(
                        success=True,
                        final_answer=final_answer,
                        thought_steps=thought_history
                    )

                if thought_step.action and thought_step.action != "无":
                    observation = await self._execute_action(
                        thought_step.action,
                        thought_step.action_input or "",
                        context
                    )

                    thought_step.observation = observation

                    thought_history[-1] = thought_step
                else:
                    if thought_step.observation:
                        pass
                    else:
                        thought_step.observation = thought_step.thought
                        thought_history[-1] = thought_step

                await asyncio.sleep(0.5)

            except Exception as e:
                _log.error(f"[ReAct] Error in iteration {step_num}: {e}")
                continue

        final_thought = thought_history[-1] if thought_history else None
        final_answer = final_thought.observation or final_thought.thought if final_thought else "抱歉，我暂时无法回答这个问题。"

        return ReActResult(
            success=False,
            final_answer=f"{final_answer}\n\n(已达到最大思考次数)",
            thought_steps=thought_history
        )

    def _parse_thought(self, content: str, step: int) -> ThoughtStep:
        """解析思考步骤"""
        thought = ""
        action = None
        action_input = None
        observation = None
        is_final = False

        lines = content.split("\n")
        current_field = None

        for line in lines:
            line = line.strip()

            if "思考:" in line:
                thought = line.split("思考:")[1].strip()
                current_field = "thought"
            elif "行动:" in line:
                action_str = line.split("行动:")[1].strip()
                if "完成" in action_str:
                    action = "完成"
                    is_final = True
                else:
                    action = action_str
                current_field = "action"
            elif "输入:" in line:
                action_input = line.split("输入:")[1].strip()
                if action_input == "无":
                    action_input = None
                current_field = "action_input"
            elif "观察结果:" in line or "观察:" in line:
                observation = line.split("观察结果:")[1].strip() if "观察结果:" in line else line.split("观察:")[1].strip()
                current_field = "observation"
            elif current_field == "thought" and line:
                thought += " " + line
            elif current_field == "observation" and line:
                observation += " " + line

        return ThoughtStep(
            step=step,
            thought=thought,
            action=action,
            action_input=action_input,
            observation=observation,
            is_final=is_final
        )

    async def _execute_action(
        self,
        action: str,
        action_input: str,
        context: SkillContext
    ) -> str:
        """执行技能行动"""
        try:
            result = await self.plugin_manager.execute_skill(
                action,
                context,
                message=action_input
            )

            if result.success:
                return result.content
            else:
                return f"技能执行失败: {result.error}"

        except Exception as e:
            _log.error(f"[ReAct] Action execution error: {e}")
            return f"执行出错: {str(e)}"


react_agent: Optional[ReActAgent] = None


def get_react_agent() -> ReActAgent:
    """获取 ReAct Agent 单例"""
    global react_agent
    if react_agent is None:
        react_agent = ReActAgent()
    return react_agent
