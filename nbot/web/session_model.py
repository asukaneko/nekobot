"""会话级模型覆盖

允许单个会话临时切换聊天模型（不影响全局 active model，也不改其它会话）。

配置结构::

    {"enabled": true, "model_id": "<ai_models[].id>"}

取配置的优先级：
1. 会话 `model_override.enabled` 且能找到对应模型 → 该模型排在候选首位；
2. `keep_fallback=True`（默认）时用同用途的其它模型兜底（故障转移仍可用）；
3. `keep_fallback=False` 时只使用覆盖模型（模型挂了就直接报错）；
4. 覆盖关闭或模型 id 找不到 → 完全回退到默认队列。
"""

from typing import Any, Dict, List, Optional


def normalize_model_override(raw: Any) -> Dict[str, Any]:
    """归一化会话级模型覆盖配置；无效输入返回空字典。"""
    if isinstance(raw, str):
        raw = {"enabled": bool(raw.strip()), "model_id": raw.strip()}
    if not isinstance(raw, dict):
        return {}

    model_id = str(raw.get("model_id") or raw.get("model") or "").strip()
    enabled = bool(raw.get("enabled", bool(model_id)))
    if not model_id:
        return {}
    return {"enabled": enabled, "model_id": model_id[:120]}


def resolve_model_configs(
    session: Optional[Dict[str, Any]],
    default_configs: List[Dict[str, Any]],
    all_configs: Optional[List[Dict[str, Any]]] = None,
    *,
    keep_fallback: bool = True,
) -> List[Dict[str, Any]]:
    """按会话覆盖配置重排候选模型队列。

    Args:
        session: 会话字典（可含 ``model_override``）
        default_configs: 默认候选（按优先级排序）
        all_configs: 可切换的模型全量列表（``data/web/ai_models.json``）
        keep_fallback: 是否保留默认队列里的其它模型作为故障转移候选
    """
    defaults = [cfg for cfg in (default_configs or []) if isinstance(cfg, dict)]
    if not isinstance(session, dict):
        return defaults

    override = normalize_model_override(session.get("model_override"))
    if not override or not override.get("enabled"):
        return defaults

    model_id = override["model_id"]
    pool = [cfg for cfg in (all_configs or []) if isinstance(cfg, dict)]
    chosen = next(
        (cfg for cfg in pool if str(cfg.get("id") or "") == model_id),
        None,
    )
    if chosen is None:
        # 覆盖目标已删除/禁用 → 回退默认队列
        return defaults

    if not keep_fallback:
        return [chosen]

    rest = [
        cfg for cfg in defaults
        if str(cfg.get("id") or "") != model_id
    ]
    return [chosen] + rest


def describe_model_override(session: Optional[Dict[str, Any]]) -> str:
    """返回用于日志/调试的一句话描述。"""
    override = normalize_model_override((session or {}).get("model_override"))
    if not override or not override.get("enabled"):
        return "默认模型队列"
    return f"会话覆盖模型 id={override['model_id']}"
