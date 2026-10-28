"""提示词宏系统（对标 SillyTavern ``{{...}}``）

在提示词/开场白/世界书文本中展开宏。设计为纯函数 + 轻量上下文，便于单测与
在不同通道（Web / QQ / Telegram / 飞书）复用。

支持的宏（名称大小写不敏感）：

===========================  ==================================================
宏                             行为
===========================  ==================================================
``{{user}}``                 当前用户名
``{{char}}``                 当前角色名
``{{time}}``                 本地时间 HH:MM
``{{date}}``                 本地日期 YYYY-MM-DD
``{{datetime}}``             本地日期时间 YYYY-MM-DD HH:MM
``{{weekday}}``              中文星期（星期一…星期日）
``{{isotime}}``              ISO 8601 时间
``{{unix}}``                 Unix 时间戳（秒）
``{{random::a::b::c}}``      从候选中随机取一个
``{{random:a,b,c}}``         逗号分隔的同义写法
``{{pick::a::b}}``           按 seed 稳定选取（同一轮结果固定）
``{{roll::2d6+1}}``          掷骰求和
``{{getvar::名字}}``         读取会话变量（不存在则空串）
``{{setvar::名字::值}}``     写入会话变量（不输出内容）
``{{addvar::名字::数值}}``   数值累加（不输出内容）
``{{delvar::名字}}``         删除变量（不输出内容）
``{{newline}}``              换行
``{{trim}}``                 删除自身两侧空白
``{{// 注释}}``              整段丢弃
===========================  ==================================================

未知宏保持原样，避免误删用户文本；嵌套宏最多展开 3 轮，防止自引用死循环。
"""

import hashlib
import logging
import os
import random
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

MAX_EXPAND_ROUNDS = 3

_MACRO_PATTERN = re.compile(r"\{\{(.*?)\}\}", re.S)
_ROLL_PATTERN = re.compile(r"^\s*(\d*)d(\d+)\s*([+-]\s*\d+)?\s*$", re.IGNORECASE)
_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


@dataclass
class MacroContext:
    """宏展开上下文。

    Attributes:
        user_name: ``{{user}}`` 的替换值，空串表示不替换。
        char_name: ``{{char}}`` 的替换值。
        variables: 会话级变量表（会被 ``setvar/addvar/delvar`` 原地修改）。
        now: 当前时间，默认取系统本地时间。
        seed: ``{{pick}}`` 的稳定种子（通常是会话 id + 轮次）。
    """

    user_name: str = ""
    char_name: str = ""
    variables: Dict[str, Any] = field(default_factory=dict)
    now: Optional[datetime] = None
    seed: str = ""

    def timestamp(self) -> datetime:
        return self.now or datetime.now().astimezone()


def _split_candidates(body: str) -> List[str]:
    if "::" in body:
        parts = body.split("::")
    else:
        parts = body.split(",")
    return [part for part in parts if part != ""]


def _stable_choice(candidates: List[str], seed: str) -> str:
    if not candidates:
        return ""
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    return candidates[int(digest[:8], 16) % len(candidates)]


def _roll(body: str) -> str:
    match = _ROLL_PATTERN.match(body or "")
    if not match:
        return ""
    count_text, sides_text, modifier_text = match.groups()
    count = int(count_text) if count_text else 1
    count = max(1, min(100, count))
    sides = max(1, min(1000, int(sides_text)))
    modifier = 0
    if modifier_text:
        modifier = int(modifier_text.replace(" ", ""))
    total = sum(random.randint(1, sides) for _ in range(count)) + modifier
    return str(total)


def _expand_once(text: str, context: MacroContext, round_index: int) -> str:
    def replace(match: "re.Match[str]") -> str:
        raw = match.group(1)
        body = raw.strip()
        if not body:
            return ""
        if body.startswith("//"):
            return ""

        name_raw, _, rest = body.partition("::")
        name = name_raw.strip().lower()

        # trim 是全文级操作，交给 expand() 统一处理
        if name == "trim":
            return ""

        # 兼容 {{random:a,b}} 这种单冒号写法
        if not rest and ":" in body:
            single_name, _, single_rest = body.partition(":")
            if single_name.strip().lower() in {"random", "pick", "roll"}:
                name, rest = single_name.strip().lower(), single_rest

        if name == "user":
            return context.user_name or match.group(0)
        if name == "char":
            return context.char_name or match.group(0)
        if name in {"newline", "nl"}:
            return "\n"

        now = context.timestamp()
        if name == "time":
            return now.strftime("%H:%M")
        if name == "date":
            return now.strftime("%Y-%m-%d")
        if name == "datetime":
            return now.strftime("%Y-%m-%d %H:%M")
        if name == "weekday":
            return _WEEKDAYS[now.weekday()]
        if name == "isotime":
            return now.isoformat()
        if name == "unix":
            return str(int(now.timestamp()))

        if name == "random":
            candidates = _split_candidates(rest) if rest else ["0", "100"]
            if not candidates:
                return ""
            return random.choice(candidates)

        if name == "pick":
            candidates = _split_candidates(rest)
            if not candidates:
                return ""
            seed = f"{context.seed}|{round_index}|{match.group(0)}"
            return _stable_choice(candidates, seed)

        if name == "roll":
            return _roll(rest)

        if name in {"getvar", "setvar", "addvar", "delvar"}:
            var_name, _, var_value = rest.partition("::")
            var_name = var_name.strip()
            if not var_name:
                return ""
            if name == "getvar":
                value = context.variables.get(var_name, "")
                return "" if value is None else str(value)
            if name == "setvar":
                context.variables[var_name] = var_value
                return ""
            if name == "addvar":
                current = context.variables.get(var_name, 0)
                try:
                    total = float(current) + float(var_value.strip() or 0)
                except (TypeError, ValueError):
                    return ""
                if total.is_integer():
                    total = int(total)
                context.variables[var_name] = total
                return ""
            context.variables.pop(var_name, None)
            return ""

        return match.group(0)

    return _MACRO_PATTERN.sub(replace, text)


def expand(text: str, context: Optional[MacroContext] = None) -> str:
    """展开文本中的宏；未知宏与不可解析的表达式保持原样。"""
    if not text or "{{" not in text:
        return text or ""
    ctx = context or MacroContext()
    wants_trim = bool(re.search(r"\{\{\s*trim\s*\}\}", text, re.IGNORECASE))
    result = text
    for round_index in range(MAX_EXPAND_ROUNDS):
        expanded = _expand_once(result, ctx, round_index)
        if expanded == result:
            break
        result = expanded
    if wants_trim:
        result = result.strip()
    return result


def has_macros(text: str) -> bool:
    return bool(text) and "{{" in text


def strip_macros(text: str) -> str:
    """移除所有宏（用于估算长度或纯文本预览）。"""
    if not text or "{{" not in text:
        return text or ""
    return _MACRO_PATTERN.sub("", text)


def build_context(
    *,
    user_name: str = "",
    char_name: str = "",
    variables: Optional[Dict[str, Any]] = None,
    seed: str = "",
    now: Optional[datetime] = None,
) -> MacroContext:
    """便捷构造上下文。"""
    return MacroContext(
        user_name=str(user_name or ""),
        char_name=str(char_name or ""),
        variables=variables if variables is not None else {},
        seed=str(seed or ""),
        now=now,
    )


# --------------------------------------------------------------------------- #
# 会话级变量持久化（{{setvar}} / {{getvar}}）
# --------------------------------------------------------------------------- #

_LOG = logging.getLogger(__name__)
_STORE = None


class MacroVariableStore:
    """按会话（scope）保存宏变量的 JSON 存储。

    数据文件：``data/macro_variables.json``，结构 ``{scope_id: {name: value}}``。
    所有通道共用同一份存储，QQ/Telegram/飞书无需额外接线。
    """

    def __init__(self, base_dir: Optional[str] = None):
        from nbot.character.storage.json_store import JsonStore

        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self._store = JsonStore(os.path.join(base_dir, "data", "macro_variables.json"))

    def get(self, scope_id: str) -> Dict[str, Any]:
        scope_id = str(scope_id or "").strip()
        if not scope_id:
            return {}
        data = self._store.get(scope_id)
        return dict(data) if isinstance(data, dict) else {}

    def save(self, scope_id: str, variables: Dict[str, Any]) -> None:
        scope_id = str(scope_id or "").strip()
        if not scope_id:
            return
        # 去掉空字典，避免无限增长
        if not variables:
            self._store.delete(scope_id)
            return
        self._store.set(scope_id, dict(variables))

    def clear(self, scope_id: str) -> bool:
        return self._store.delete(str(scope_id or ""))


def get_variable_store(base_dir: Optional[str] = None) -> MacroVariableStore:
    """返回进程内共享的变量存储（首次调用时创建）。"""
    global _STORE
    if _STORE is None or base_dir is not None:
        _STORE = MacroVariableStore(base_dir)
    return _STORE
