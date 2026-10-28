"""世界模拟状态增量工具

世界引擎每回合只产出「增量」（如 ``{"global_state": {"tension": "+5"}}``），
由这里统一应用到节点状态上。支持：

- 数值增量：``"+4"`` / ``"-3"``（字符串形式，便于模型直接输出）
- 列表追加：``"memory_append": ["..."]``
- 列表移除：``"memory_remove": ["..."]``
- 嵌套字典：递归合并，未出现的键保持不变
"""

import copy
from typing import Any, Dict, List

APPEND_SUFFIX = "_append"
REMOVE_SUFFIX = "_remove"


def _as_number(value: Any):
    """把数字或数字字符串转成 int/float，失败返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    return None


def _apply_scalar(current: Any, value: Any) -> Any:
    """应用单个标量增量：``"+N"``/``"-N"`` 相对当前值，其他直接覆盖。"""
    if isinstance(value, str) and len(value) > 1 and value[0] in "+-":
        delta = _as_number(value[1:])
        if delta is not None:
            base = _as_number(current)
            base = 0 if base is None else base
            return base + (delta if value[0] == "+" else -delta)
    return copy.deepcopy(value)


def _as_items(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [copy.deepcopy(item) for item in value]
    return [copy.deepcopy(value)]


def apply_state_delta(state: Dict[str, Any], delta: Dict[str, Any]) -> Dict[str, Any]:
    """把 delta 应用到 state 上并返回新状态（不修改入参）。"""
    result: Dict[str, Any] = copy.deepcopy(state) if isinstance(state, dict) else {}
    if not isinstance(delta, dict):
        return result

    for key, value in delta.items():
        if not isinstance(key, str):
            continue
        if key.endswith(APPEND_SUFFIX) and len(key) > len(APPEND_SUFFIX):
            target = key[: -len(APPEND_SUFFIX)]
            bucket = result.get(target)
            if not isinstance(bucket, list):
                bucket = []
            bucket.extend(_as_items(value))
            result[target] = bucket
        elif key.endswith(REMOVE_SUFFIX) and len(key) > len(REMOVE_SUFFIX):
            target = key[: -len(REMOVE_SUFFIX)]
            bucket = result.get(target)
            if isinstance(bucket, list):
                dropped = _as_items(value)
                result[target] = [item for item in bucket if item not in dropped]
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = apply_state_delta(result[key], value)
        else:
            result[key] = _apply_scalar(result.get(key), value)
    return result
