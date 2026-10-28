"""成就定义清单（对齐 Android 本地模式的成就体系）"""

from __future__ import annotations

from nbot.achievements.models import AchievementDef

# 类别显示顺序
CATEGORY_ORDER = [
    "conversation",
    "session",
    "token",
    "cost",
    "character",
    "worldbook",
    "memory",
    "plot",
    "extension",
    "system",
]

CATEGORY_LABELS = {
    "conversation": "对话",
    "session": "会话",
    "token": "Token 用量",
    "cost": "累计消费",
    "character": "角色",
    "worldbook": "世界书",
    "memory": "记忆",
    "plot": "剧情",
    "extension": "拓展",
    "system": "系统",
}


def _a(aid, name, desc, icon, category, stat_key, target, unit=""):
    return AchievementDef(
        id=aid, name=name, description=desc, icon=icon,
        category=category, stat_key=stat_key, target=target, unit=unit,
    )


ACHIEVEMENT_DEFS: list[AchievementDef] = [
    # ---- 对话 ----
    _a("msg_first", "初次对话", "完成第一次 AI 对话", "fas fa-comment-dots", "conversation", "messages", 1, "条"),
    _a("msg_10", "消息达人·10", "累计发送 10 条消息", "fas fa-comments", "conversation", "messages", 10, "条"),
    _a("msg_100", "消息达人·100", "累计发送 100 条消息", "fas fa-comments", "conversation", "messages", 100, "条"),
    _a("msg_1000", "消息达人·1000", "累计发送 1000 条消息", "fas fa-comment-medical", "conversation", "messages", 1000, "条"),
    _a("msg_10000", "消息达人·1万", "累计发送 1 万条消息", "fas fa-fire", "conversation", "messages", 10000, "条"),

    # ---- 会话 ----
    _a("session_first", "会话初开", "创建第一个会话", "fas fa-folder-open", "session", "sessions", 1, "个"),
    _a("session_10", "会话积累·10", "累计创建 10 个会话", "fas fa-folder", "session", "sessions", 10, "个"),
    _a("session_50", "会话积累·50", "累计创建 50 个会话", "fas fa-folder", "session", "sessions", 50, "个"),
    _a("session_100", "会话积累·100", "累计创建 100 个会话", "fas fa-folder-tree", "session", "sessions", 100, "个"),

    # ---- Token ----
    _a("token_1m", "百万级 Token", "累计消耗 100 万 Token", "fas fa-microchip", "token", "tokens", 1_000_000, ""),
    _a("token_10m", "千万级 Token", "累计消耗 1000 万 Token", "fas fa-microchip", "token", "tokens", 10_000_000, ""),
    _a("token_100m", "亿级 Token", "累计消耗 1 亿 Token", "fas fa-brain", "token", "tokens", 100_000_000, ""),

    # ---- 消费 ----
    _a("cost_1", "初尝付费", "累计消费 ¥1", "fas fa-yen-sign", "cost", "cost", 1.0, "元"),
    _a("cost_10", "消费达人·10", "累计消费 ¥10", "fas fa-yen-sign", "cost", "cost", 10.0, "元"),
    _a("cost_100", "消费达人·100", "累计消费 ¥100", "fas fa-gem", "cost", "cost", 100.0, "元"),

    # ---- 角色 ----
    _a("char_first", "角色初遇", "创建第一个角色卡", "fas fa-id-card", "character", "characters", 1, "个"),
    _a("char_5", "角色收藏家·5", "拥有 5 个角色卡", "fas fa-id-card", "character", "characters", 5, "个"),
    _a("char_10", "角色收藏家·10", "拥有 10 个角色卡", "fas fa-users", "character", "characters", 10, "个"),
    _a("affection_90", "心有灵犀", "与某个角色的好感度达到 90", "fas fa-heart", "character", "affection_max", 90, "点"),
    _a("affection_100", "情投意合", "与某个角色的好感度达到 100", "fas fa-heart", "character", "affection_max", 100, "点"),

    # ---- 世界书 ----
    _a("wb_first", "世界初构", "创建第一个世界书", "fas fa-globe", "worldbook", "worldbooks", 1, "本"),
    _a("wb_5", "世界观构筑者·5", "拥有 5 本世界书", "fas fa-globe-asia", "worldbook", "worldbooks", 5, "本"),
    _a("wb_20", "世界观构筑者·20", "拥有 20 本世界书", "fas fa-globe-africa", "worldbook", "worldbooks", 20, "本"),

    # ---- 记忆 ----
    _a("memory_first", "记忆初存", "保存第一条记忆", "fas fa-brain", "memory", "memories", 1, "条"),
    _a("memory_50", "记忆宝库·50", "累计保存 50 条记忆", "fas fa-brain", "memory", "memories", 50, "条"),

    # ---- 剧情 ----
    _a("plot_first", "剧情开端", "创建第一个剧情节点", "fas fa-project-diagram", "plot", "plot_nodes", 1, "个"),
    _a("plot_10", "剧情大师·10", "累计创建 10 个剧情节点", "fas fa-sitemap", "plot", "plot_nodes", 10, "个"),

    # ---- 拓展 ----
    _a("mcp_first", "外接世界", "配置第一个 MCP 服务器", "fas fa-server", "extension", "mcp_servers", 1, "个"),
    _a("skill_first", "技能初装", "安装第一个技能", "fas fa-magic", "extension", "skills", 1, "个"),
    _a("hook_first", "自动化初探", "配置第一条 Hook", "fas fa-anchor", "extension", "hooks", 1, "条"),
    _a("oauth_first", "快捷登录", "通过 OAuth 登录 AI 服务商", "fas fa-shield-alt", "extension", "oauth_accounts", 1, "个"),

    # ---- 系统 ----
    _a("webdav_first", "云端堡垒", "完成首次 WebDAV 备份", "fas fa-cloud-upload-alt", "system", "webdav_backup", 1, ""),
    _a("failover_first", "双保险", "配置多模型故障转移", "fas fa-random", "system", "failover_models", 2, "个"),
    _a("smart_routing_first", "智能调度", "开启智能模型路由", "fas fa-route", "system", "smart_routing", 1, ""),
    _a("review_first", "复盘初心", "完成首次对话复盘", "fas fa-clipboard-check", "system", "reviews", 1, "次"),
]


def get_achievement_defs() -> list[AchievementDef]:
    return list(ACHIEVEMENT_DEFS)


def get_achievement_def(aid: str) -> AchievementDef | None:
    for d in ACHIEVEMENT_DEFS:
        if d.id == aid:
            return d
    return None
