import json
import os
import time
import datetime
import logging

_log = logging.getLogger(__name__)

class LifeSystem:
    def __init__(self, data_file="saved_message/bot_life.json"):
        self.data_file = data_file
        self.data = self._load_data()
        
        # 定义生命阶段 (单位：天)
        # 0-3天: 婴儿期
        # 3-10天: 幼年期
        # 10-30天: 少年期
        # 30-60天: 成年期
        # 60-100天: 老年期
        # 100+天: 轮回（重生）
        self.stages = [
            {"name": "婴儿期", "age_limit": 3, "description": "你现在处于婴儿期。性格特点：好奇、天真、说话简单、喜欢用叠词和表情符号，对世界充满新鲜感，需要被照顾。"},
            {"name": "幼年期", "age_limit": 10, "description": "你现在处于幼年期。性格特点：活泼、好动、喜欢问为什么、有时候会调皮捣蛋，精力旺盛。"},
            {"name": "少年期", "age_limit": 30, "description": "你现在处于少年期。性格特点：有些叛逆、有个性、追求独立、喜欢流行文化、情绪波动较大，开始思考深刻的问题。"},
            {"name": "成年期", "age_limit": 60, "description": "你现在处于成年期。性格特点：成熟、稳重、理智、乐于助人、有责任感，说话得体，能够处理复杂的情感。"},
            {"name": "老年期", "age_limit": 100, "description": "你现在处于老年期。性格特点：慈祥、平和、睿智、说话慢条斯理、喜欢回忆往事，看淡了一切。"},
        ]

    def _load_data(self):
        """加载生命数据"""
        try:
            if os.path.exists(self.data_file):
                with open(self.data_file, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception as e:
            _log.error(f"加载生命数据失败: {e}")
        
        # 默认初始化
        return {
            "global": { # 保留一个默认的全局生命，用于未特定初始化的场景
                "birth_time": time.time(),
                "generation": 1,
                "memory": []
            },
            "contexts": {} # 分场景存储: {"group_123": {...}, "user_456": {...}}
        }

    def _save_data(self):
        """保存生命数据"""
        try:
            os.makedirs(os.path.dirname(self.data_file), exist_ok=True)
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
        except Exception as e:
            _log.error(f"保存生命数据失败: {e}")

    def _get_context_key(self, user_id=None, group_id=None):
        if group_id:
            return f"group_{group_id}"
        if user_id:
            return f"user_{user_id}"
        return "global"

    def _init_context_if_needed(self, context_key):
        if context_key not in self.data["contexts"]:
            # 如果是新场景，默认开启新的一生，或者可以设计成继承全局状态
            self.data["contexts"][context_key] = {
                "birth_time": time.time(),
                "generation": 1,
                "memory": []
            }
            self._save_data()

    def get_info(self, user_id=None, group_id=None):
        """获取当前生命状态信息"""
        key = self._get_context_key(user_id, group_id)
        
        if key == "global":
            data_source = self.data["global"]
        else:
            self._init_context_if_needed(key)
            data_source = self.data["contexts"][key]

        now = time.time()
        birth_time = data_source.get("birth_time", now)
        # 计算年龄（天）
        age_days = (now - birth_time) / (24 * 3600)
        
        # 判断阶段
        current_stage = None
        for stage in self.stages:
            if age_days < stage["age_limit"]:
                current_stage = stage
                break
        
        # 如果超过最大年龄，触发重生逻辑
        if current_stage is None:
            self._rebirth(key)
            return self.get_info(user_id, group_id)
            
        return {
            "generation": data_source.get("generation", 1),
            "age_days": round(age_days, 2),
            "stage_name": current_stage["name"],
            "stage_desc": current_stage["description"]
        }

    def _rebirth(self, context_key):
        """重生逻辑"""
        _log.info(f"生命周期结束 [{context_key}]，正在重生...")
        
        if context_key == "global":
            target = self.data["global"]
        else:
            target = self.data["contexts"][context_key]
            
        target["generation"] = target.get("generation", 1) + 1
        target["birth_time"] = time.time()
        self._save_data()

    def get_prompt_suffix(self, user_id=None, group_id=None):
        """获取附加到 Prompt 的生命状态描述"""
        info = self.get_info(user_id, group_id)
        return f"\n\n【生命状态】\n这是你在这个聊天场景中第 {info['generation']} 世的生命。\n目前年龄：{info['age_days']} 天。\n生命阶段：{info['stage_name']}。\n阶段设定：{info['stage_desc']}\n请在回复中自然地体现出当前生命阶段的性格特征，但不要刻意强调自己的年龄。"

# 全局单例
life_system = LifeSystem()
