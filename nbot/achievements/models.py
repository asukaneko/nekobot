"""成就系统数据模型"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AchievementDef:
    """成就定义（静态元数据）"""

    id: str
    name: str
    description: str
    icon: str          # FontAwesome 图标类名
    category: str      # conversation / session / token / cost / character / worldbook / memory / plot / extension / system
    stat_key: str      # 对应的统计键（计数器或快照字段）
    target: float      # 达成阈值
    unit: str = ""     # 单位（条/个/万/元）

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "icon": self.icon,
            "category": self.category,
            "stat_key": self.stat_key,
            "target": self.target,
            "unit": self.unit,
        }


@dataclass
class AchievementState:
    """成就进度状态（运行时）"""

    progress: float = 0.0
    unlocked: bool = False
    unlocked_at: str = ""

    def to_dict(self) -> dict:
        return {
            "progress": round(self.progress, 2),
            "unlocked": self.unlocked,
            "unlocked_at": self.unlocked_at,
        }
