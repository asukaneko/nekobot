"""
nbot.world

世界模拟子系统，两个互不相同的「世界引擎」共处一个包：

- ``nbot.world.engine``：群聊环境判定器（谁该说话 / 要不要旁白），
  入口 ``get_world_engine()``，无状态、不落盘。
- ``nbot.world.simulation.WorldSimulation``：世界模拟引擎
  （``step`` / ``tick`` / ``jump_time``），由 ``WorldStore`` 持久化驱动。

历史原因两者都曾叫 ``WorldEngine``，为避免歧义，本包不再导出 ``WorldEngine`` 这个名字，
请显式使用 ``WorldSimulation`` 或 ``get_world_engine()``。
"""

from nbot.world.character_resolver import CharacterResolver
from nbot.world.engine import WorldEngineDecision, get_world_engine
from nbot.world.simulation import WorldSimulation
from nbot.world.state_diff import apply_state_delta
from nbot.world.store import WorldStore

__all__ = [
    "CharacterResolver",
    "WorldEngineDecision",
    "WorldSimulation",
    "WorldStore",
    "apply_state_delta",
    "get_world_engine",
]
