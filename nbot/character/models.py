"""
角色引擎核心数据模型

定义角色卡、角色状态、关系状态、角色记忆、反应计划等数据结构。
与旧 personality.json 格式兼容，同时支持新架构的独立存储。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


DEFAULT_INITIAL_STATE: Dict[str, Any] = {
    "affection": 50,
    "trust": 50,
    "familiarity": 30,
    "dependency": 30,
    "security": 50,
    "jealousy": 0,
    "mood": "开心",
}


_INITIAL_STATE_CONTAINER_KEYS = (
    "state",
    "initial_state",
    "initialState",
    "runtime_state",
    "runtimeState",
)


_RELATIONSHIP_CONTAINER_KEYS = (
    "relationship",
    "relationships",
    "initial_relationship",
    "initialRelationship",
    "initial_relation",
    "initialRelation",
)


_INITIAL_STATE_FIELD_ALIASES = {
    "affection": "affection",
    "好感": "affection",
    "好感度": "affection",
    "trust": "trust",
    "信任": "trust",
    "信任度": "trust",
    "familiarity": "familiarity",
    "熟悉": "familiarity",
    "熟悉度": "familiarity",
    "dependency": "dependency",
    "dependence": "dependency",
    "依赖": "dependency",
    "依赖度": "dependency",
    "security": "security",
    "安全感": "security",
    "jealousy": "jealousy",
    "嫉妒": "jealousy",
    "嫉妒心": "jealousy",
    "mood": "mood",
    "心情": "mood",
    "当前心情": "mood",
    "energy": "energy",
    "精力": "energy",
    "mood_intensity": "mood_intensity",
    "emotion_intensity": "mood_intensity",
    "情绪强度": "mood_intensity",
}


def _normalize_initial_state_value(key: str, value: Any) -> Any:
    if key == "mood":
        return str(value)
    if key == "mood_intensity":
        try:
            return max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            return value
    if key in {"affection", "trust", "familiarity", "dependency", "security", "jealousy", "energy"}:
        try:
            return max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return value
    return value


def _merge_initial_state_fields(target: Dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for raw_key, value in source.items():
        field_name = _INITIAL_STATE_FIELD_ALIASES.get(str(raw_key), str(raw_key))
        if field_name in _INITIAL_STATE_FIELD_ALIASES.values():
            target[field_name] = _normalize_initial_state_value(field_name, value)


def normalize_character_initial_state(data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a complete initial runtime state from supported card formats."""
    initial_state = dict(DEFAULT_INITIAL_STATE)

    for key in _INITIAL_STATE_CONTAINER_KEYS:
        _merge_initial_state_fields(initial_state, data.get(key))

    for key in _RELATIONSHIP_CONTAINER_KEYS:
        _merge_initial_state_fields(initial_state, data.get(key))

    _merge_initial_state_fields(initial_state, data)
    return initial_state


@dataclass
class CharacterProfile:
    """静态角色卡，描述角色的固定设定"""

    id: str = ""
    name: str = ""
    version: int = 1

    description: str = ""
    avatar: str = ""
    portrait: str = ""
    tags: List[str] = field(default_factory=list)

    basic_info: str = ""
    personality: str = ""
    scenario: str = ""
    first_message: str = ""
    example_dialogues: str = ""
    response_format: str = ""
    rules: List[str] = field(default_factory=list)

    initial_state: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    # 旧字段兼容：systemPrompt 在旧 personality 中存在
    system_prompt: str = ""

    @classmethod
    def from_personality_dict(cls, data: Dict[str, Any]) -> "CharacterProfile":
        """从旧 personality.json 格式转换"""
        profile = cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            avatar=data.get("avatar", ""),
            portrait=data.get("portrait", ""),
            tags=data.get("tags", []),
            basic_info=data.get("basicInfo", ""),
            personality=data.get("personality", ""),
            scenario=data.get("scenario", ""),
            first_message=data.get("firstMessage", ""),
            example_dialogues=data.get("exampleDialogues", ""),
            response_format=data.get("responseFormat", ""),
            rules=data.get("rules", []),
            system_prompt=data.get("systemPrompt", ""),
            initial_state=normalize_character_initial_state(data),
            metadata={
                "greeting": data.get("greeting", ""),
            },
        )
        return profile

    def to_personality_dict(self) -> Dict[str, Any]:
        """转换为旧 personality.json 格式（兼容旧 API）"""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "avatar": self.avatar,
            "portrait": self.portrait,
            "tags": self.tags,
            "basicInfo": self.basic_info,
            "personality": self.personality,
            "scenario": self.scenario,
            "firstMessage": self.first_message,
            "exampleDialogues": self.example_dialogues,
            "responseFormat": self.response_format,
            "rules": self.rules,
            "state": self.initial_state,
            "systemPrompt": self.system_prompt,
            "greeting": self.metadata.get("greeting", ""),
        }


@dataclass
class CharacterState:
    """角色运行时状态，每个 scope_id（会话/用户）独立"""

    character_id: str = ""
    scope_id: str = ""

    mood: str = "平静"
    mood_intensity: float = 0.5
    energy: int = 70

    scene: Dict[str, Any] = field(default_factory=dict)
    last_active_at: str = ""
    updated_at: str = ""

    # 性格演化：经历塑造人格偏移，不修改 profile.personality 本身
    # 结构示例: [{"trait": "openness", "delta": -5, "reason": "长期被忽视后更内向", "turn": 42}]
    personality_evolution: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "character_id": self.character_id,
            "scope_id": self.scope_id,
            "mood": self.mood,
            "mood_intensity": self.mood_intensity,
            "energy": self.energy,
            "scene": self.scene,
            "last_active_at": self.last_active_at,
            "updated_at": self.updated_at,
            "personality_evolution": self.personality_evolution,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CharacterState":
        return cls(
            character_id=data.get("character_id", ""),
            scope_id=data.get("scope_id", ""),
            mood=data.get("mood", "平静"),
            mood_intensity=data.get("mood_intensity", 0.5),
            energy=data.get("energy", 70),
            scene=data.get("scene", {}),
            last_active_at=data.get("last_active_at", ""),
            updated_at=data.get("updated_at", ""),
            personality_evolution=data.get("personality_evolution", []),
        )


@dataclass
class RelationshipState:
    """角色与目标用户的关系状态"""

    character_id: str = ""
    target_id: str = ""

    affection: int = 50
    trust: int = 50
    familiarity: int = 30
    dependency: int = 30
    security: int = 50
    jealousy: int = 0

    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "character_id": self.character_id,
            "target_id": self.target_id,
            "affection": self.affection,
            "trust": self.trust,
            "familiarity": self.familiarity,
            "dependency": self.dependency,
            "security": self.security,
            "jealousy": self.jealousy,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RelationshipState":
        return cls(
            character_id=data.get("character_id", ""),
            target_id=data.get("target_id", ""),
            affection=data.get("affection", 50),
            trust=data.get("trust", 50),
            familiarity=data.get("familiarity", 30),
            dependency=data.get("dependency", 30),
            security=data.get("security", 50),
            jealousy=data.get("jealousy", 0),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class CharacterMemory:
    """角色记忆条目"""

    id: str = ""
    character_id: str = ""
    target_id: str = ""

    type: str = "long"
    title: str = ""
    summary: str = ""
    content: str = ""

    importance: int = 5
    emotion_impact: Dict[str, Any] = field(default_factory=dict)
    source_turn_id: Optional[str] = None

    created_at: str = ""
    expires_at: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "character_id": self.character_id,
            "target_id": self.target_id,
            "type": self.type,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "importance": self.importance,
            "emotion_impact": self.emotion_impact,
            "source_turn_id": self.source_turn_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


@dataclass
class ReactionPlan:
    """本轮反应计划，由 ReactionPlanner 在 before_turn 中生成"""

    intent: str = "respond"
    tone: str = "natural"

    visible_emotion: str = "平静"
    hidden_emotion: str = ""

    should_reference_memory: bool = False
    memory_ids: List[str] = field(default_factory=list)

    style_controls: Dict[str, Any] = field(default_factory=dict)
    state_deltas: Dict[str, Any] = field(default_factory=dict)
    relationship_deltas: Dict[str, Any] = field(default_factory=dict)


def _clamp_probability(value: Any) -> int:
    """把触发概率夹到 0-100，非数字回落为 100（旧数据无此字段）。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 100
    return max(0, min(100, number))


@dataclass
class WorldBookEntry:
    """世界书条目，包含关键词和对应注入内容"""

    id: str = ""
    name: str = ""
    keywords: List[str] = field(default_factory=list)
    content: str = ""
    enabled: bool = True
    priority: int = 0
    case_sensitive: bool = False
    match_mode: str = "any"  # "any" = 任一命中, "all" = 全部命中

    # 多源召回扩展字段
    trigger_sources: List[str] = field(default_factory=lambda: ["user"])
    always_on: bool = False
    state_triggers: Dict[str, List[str]] = field(default_factory=dict)
    cooldown_turns: int = 0
    max_injections_per_session: int = 0
    tags: List[str] = field(default_factory=list)
    entry_type: str = "lore"
    weight: int = 0

    # 注入位置：""=默认槽（PromptStack world_book 槽）
    # before_char/after_char=角色基础提示词 前/后；
    # before_an/after_an=距对话末尾 depth 条处的锚点 前/后；
    # at_depth=按 depth 插入消息序列
    position: str = ""
    depth: int = 4

    # 酒馆卡增强：命中后的触发概率（0-100）、关键词按正则解析、整词匹配
    probability: int = 100
    use_regex: bool = False
    match_whole_words: bool = False

    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "keywords": self.keywords,
            "content": self.content,
            "enabled": self.enabled,
            "priority": self.priority,
            "case_sensitive": self.case_sensitive,
            "match_mode": self.match_mode,
            "trigger_sources": self.trigger_sources,
            "always_on": self.always_on,
            "state_triggers": self.state_triggers,
            "cooldown_turns": self.cooldown_turns,
            "max_injections_per_session": self.max_injections_per_session,
            "tags": self.tags,
            "entry_type": self.entry_type,
            "weight": self.weight,
            "position": self.position,
            "depth": self.depth,
            "probability": self.probability,
            "use_regex": self.use_regex,
            "match_whole_words": self.match_whole_words,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorldBookEntry":
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            keywords=data.get("keywords", []),
            content=data.get("content", ""),
            enabled=data.get("enabled", True),
            priority=data.get("priority", 0),
            case_sensitive=data.get("case_sensitive", False),
            match_mode=data.get("match_mode", "any"),
            trigger_sources=data.get("trigger_sources", ["user"]),
            always_on=data.get("always_on", False),
            state_triggers=data.get("state_triggers", {}),
            cooldown_turns=data.get("cooldown_turns", 0),
            max_injections_per_session=data.get("max_injections_per_session", 0),
            tags=data.get("tags", []),
            entry_type=data.get("entry_type", "lore"),
            weight=data.get("weight", 0),
            position=data.get("position", ""),
            depth=data.get("depth", 4),
            probability=_clamp_probability(data.get("probability", 100)),
            use_regex=bool(data.get("use_regex", False)),
            match_whole_words=bool(data.get("match_whole_words", False)),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class WorldBook:
    """世界书，世界观设定集合，关联多个角色"""

    id: str = ""
    name: str = ""
    description: str = ""
    cover_url: str = ""
    character_ids: List[str] = field(default_factory=list)
    entries: List[WorldBookEntry] = field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "cover_url": self.cover_url,
            "character_ids": self.character_ids,
            "entries": [e.to_dict() for e in self.entries],
            "enabled": self.enabled,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "WorldBook":
        entries = [WorldBookEntry.from_dict(e) for e in data.get("entries", [])]
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            cover_url=data.get("cover_url", data.get("coverUrl", "")),
            character_ids=data.get("character_ids", []),
            entries=entries,
            enabled=data.get("enabled", True),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class CharacterIdentity:
    """角色身份标识，用于在 Pipeline 中传递角色上下文"""

    character_id: str = ""
    target_id: str = ""
    scope_id: str = ""
    channel: str = ""


@dataclass
class CharacterTurnContext:
    """角色运行时每轮上下文，包含 before_turn 的全部产出"""

    profile: CharacterProfile = field(default_factory=CharacterProfile)
    state: CharacterState = field(default_factory=CharacterState)
    relationship: RelationshipState = field(default_factory=RelationshipState)
    memories: List[CharacterMemory] = field(default_factory=list)
    signals: Any = None
    plan: ReactionPlan = field(default_factory=ReactionPlan)
    prompt_text: str = ""
    world_book_entries: list = field(default_factory=list)
