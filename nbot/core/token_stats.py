"""
统一的 Token 用量统计管理器。

提供线程安全的用量记录、历史查询、排行榜生成和费用估算。
"""

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


# ====================================================================
# 用途分类常量（粗粒度）
# ====================================================================
PURPOSE_CHAT = "chat"                  # 主要对话（Web、QQ、Telegram等）
PURPOSE_DECISION = "decision"          # 决策类（should_reply、should_search等）
PURPOSE_MEMORY = "memory"              # 记忆相关（auto_memory、session summary等）
PURPOSE_VISION = "vision"              # 视觉理解（image、video、gif描述）
PURPOSE_UTILITY = "utility"            # 工具类（translation、summarize、analyze_json等）
PURPOSE_WEB_FEATURE = "web_feature"    # Web功能（personality、live2d、world_book等）
PURPOSE_REACT = "react"                # ReAct推理循环
PURPOSE_PLOT = "plot"                  # 剧情相关（choice_generator）
PURPOSE_HEARTBEAT = "heartbeat"        # 心跳
PURPOSE_EMBEDDING = "embedding"        # 知识库embedding
PURPOSE_TTS = "tts"                    # 语音合成
PURPOSE_IMAGE_GEN = "image_gen"        # 图片生成
PURPOSE_COMPRESSION = "compression"    # 上下文自动/手动压缩摘要
PURPOSE_AGENT_MEMORY = "agent_memory"  # Agent 会话记忆改写
PURPOSE_PLUGIN = "plugin"              # 插件/技能内部调用

# 用途显示名称映射（用于前端展示）
PURPOSE_LABELS: Dict[str, str] = {
    PURPOSE_CHAT: "对话",
    PURPOSE_DECISION: "决策",
    PURPOSE_MEMORY: "记忆",
    PURPOSE_VISION: "视觉",
    PURPOSE_UTILITY: "工具",
    PURPOSE_WEB_FEATURE: "Web功能",
    PURPOSE_REACT: "推理",
    PURPOSE_PLOT: "剧情",
    PURPOSE_HEARTBEAT: "心跳",
    PURPOSE_EMBEDDING: "向量化",
    PURPOSE_TTS: "语音合成",
    PURPOSE_IMAGE_GEN: "图片生成",
    PURPOSE_COMPRESSION: "压缩",
    PURPOSE_AGENT_MEMORY: "Agent记忆",
    PURPOSE_PLUGIN: "插件",
}


# 模型定价 ($/M tokens)
_MODEL_PRICING = {
    "claude-opus-4-7": (15.0, 75.0),       # input $15/M, output $75/M
    "claude-opus-4-5": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-3.5-sonnet": (3.0, 15.0),
    "claude-3.5-haiku": (0.80, 4.0),
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4-turbo": (10.0, 30.0),
    "deepseek-v3": (0.27, 1.10),
    "deepseek-r1": (0.55, 2.19),
}


# 费用币种统一口径：用户自填价格按人民币（元/百万token）计，
# 内置兜底定价表为美元（$/百万token），估算时按此汇率折算为人民币。
USD_TO_CNY_RATE = 7.2

# 缓存命中 token 的默认折扣（相对输入价）。
# OpenAI 缓存输入通常为输入价的 50%，Anthropic cache read 约 10%；
# 未单独配置 cached_input_price 时按 50% 估算，模型配置里可精确覆盖。
CACHE_PRICE_FALLBACK_RATIO = 0.5


def _estimate_cost(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    input_price: Optional[float] = None,
    output_price: Optional[float] = None,
    cached_tokens: int = 0,
    cached_input_price: Optional[float] = None,
) -> float:
    """估算费用（统一返回人民币元）。

    优先使用传入的 input_price/output_price（人民币 元/百万token），
    未传入时从 _MODEL_PRICING 兜底（单位：美元/百万token，按 USD_TO_CNY_RATE 折算）。

    缓存命中部分按 cached_input_price 计价；未配置时按
    CACHE_PRICE_FALLBACK_RATIO 折扣估算（缓存 token 仍计入总 token）。
    """
    in_p = input_price if isinstance(input_price, (int, float)) else None
    out_p = output_price if isinstance(output_price, (int, float)) else None

    rate_applied = False
    if in_p is None or out_p is None:
        in_p, out_p = _MODEL_PRICING.get(model, (1.0, 4.0))
        rate_applied = True

    try:
        cached = max(0, min(int(cached_tokens or 0), int(prompt_tokens or 0)))
    except (TypeError, ValueError):
        cached = 0
    uncached = max(0, int(prompt_tokens or 0) - cached)

    if cached:
        if isinstance(cached_input_price, (int, float)):
            cache_p = float(cached_input_price)
        else:
            cache_p = float(in_p) * CACHE_PRICE_FALLBACK_RATIO
    else:
        cache_p = 0.0

    cost = (
        (uncached / 1_000_000) * in_p
        + (cached / 1_000_000) * cache_p
        + (completion_tokens / 1_000_000) * out_p
    )
    if rate_applied:
        cost *= USD_TO_CNY_RATE
    return cost


class TokenStatsManager:
    """线程安全的 Token 用量统计管理器。"""

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, "token_stats.json")
        self._lock = threading.Lock()
        self._stats: Dict[str, Any] = {
            "today": 0,
            "month": 0,
            "total": 0,
            "estimated_cost": 0.0,
            "history": [],
            "sessions": {},
            "models": {},
            "users": {},
            "records": [],
        }
        self._load()

    # ------------------------------------------------------------------
    # 加载 / 保存
    # ------------------------------------------------------------------

    def _load(self):
        """从磁盘加载统计数据，自动合并同日重复条目。"""
        os.makedirs(self._data_dir, exist_ok=True)
        if not os.path.exists(self._file_path):
            return

        try:
            with open(self._file_path, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            return

        with self._lock:
            # 合并同日重复条目
            raw_history = saved.get("history", [])
            self._stats["history"] = self._merge_history(raw_history)

            today_str = datetime.now().strftime("%Y-%m-%d")
            last_history_date = self._stats["history"][-1]["date"] if self._stats["history"] else ""

            # today 计数器：如果最后一条历史不是今天，说明跨天了，重置为 0
            if last_history_date != today_str:
                self._stats["today"] = 0
            else:
                self._stats["today"] = saved.get("today", 0)

            # month 同理：检查最后一条历史的月份
            current_month = datetime.now().strftime("%Y-%m")
            last_month = last_history_date[:7] if last_history_date else ""
            if last_month != current_month:
                self._stats["month"] = 0
            else:
                self._stats["month"] = saved.get("month", 0)

            self._stats["total"] = saved.get("total", 0)
            self._stats["estimated_cost"] = float(saved.get("estimated_cost", 0) or 0)
            self._stats["sessions"] = saved.get("sessions", {})
            self._stats["models"] = saved.get("models", {})
            self._stats["users"] = saved.get("users", {})
            records = saved.get("records", [])
            self._stats["records"] = records if isinstance(records, list) else []

    def _save(self):
        """持久化到磁盘（调用前需持有锁）。"""
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump(self._stats, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _merge_history(history: List[Dict]) -> List[Dict]:
        """将同日多条记录合并为一条，去重防重复。

        旧代码产生的重复条目特征：message_count=0 且 input==output（字符估算）。
        优先保留 message_count>0 的真实 API 记录。
        """
        merged: Dict[str, List[Dict]] = {}
        for entry in history:
            date = entry.get("date", "")
            if not date:
                continue
            if date not in merged:
                merged[date] = []
            merged[date].append(entry)

        result = []
        for date, entries in merged.items():
            # 优先选择 message_count > 0 的条目（真实 API 记录）
            real_entries = [e for e in entries if e.get("message_count", 0) > 0]
            if real_entries:
                # 合并所有真实条目
                combined = {
                    "date": date,
                    "input": sum(e.get("input", 0) for e in real_entries),
                    "output": sum(e.get("output", 0) for e in real_entries),
                    "total": sum(e.get("total", 0) for e in real_entries),
                    "message_count": sum(e.get("message_count", 0) for e in real_entries),
                    "cost": sum(float(e.get("cost", 0) or 0) for e in real_entries),
                }
            else:
                # 没有真实记录则取最大值合并
                combined = {
                    "date": date,
                    "input": max(e.get("input", 0) for e in entries),
                    "output": max(e.get("output", 0) for e in entries),
                    "total": max(e.get("total", 0) for e in entries),
                    "message_count": max(e.get("message_count", 0) for e in entries),
                    "cost": max(float(e.get("cost", 0) or 0) for e in entries),
                }
            result.append(combined)

        return sorted(result, key=lambda x: x["date"])

    @staticmethod
    def _filter_records(
        records: List[Dict],
        date_range: str,
        start_date: str = "",
        end_date: str = "",
    ) -> List[Dict]:
        if start_date or end_date:
            start = start_date or "0000-00-00"
            end = end_date or "9999-99-99"
            return [
                r for r in records
                if start <= (r.get("date") or "") <= end
            ]

        today_str = datetime.now().strftime("%Y-%m-%d")
        if date_range == "today":
            return [r for r in records if r.get("date") == today_str]
        # "week"/"7d"：最近 7 天（前端"本周"选项发送 week）
        if date_range in ("week", "7d"):
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            return [r for r in records if (r.get("date") or "") >= cutoff]
        if date_range == "30d":
            cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            return [r for r in records if (r.get("date") or "") >= cutoff]
        return list(records)

    def get_records(
        self,
        date_range: str = "today",
        *,
        start_date: str = "",
        end_date: str = "",
    ) -> List[Dict[str, Any]]:
        """返回指定时间段内的全部明细记录。"""
        with self._lock:
            records = list(self._stats.get("records", []))

        filtered = self._filter_records(records, date_range, start_date, end_date)
        return sorted(filtered, key=lambda item: item.get("timestamp", ""))

    # ------------------------------------------------------------------
    # 记录用量
    # ------------------------------------------------------------------

    def record_usage(
        self,
        prompt_tokens: int,
        completion_tokens: int,
        *,
        total_tokens: Optional[int] = None,
        model: str = "",
        session_id: str = "",
        channel_type: str = "web",
        user_id: str = "",
        source: str = "api",
        purpose: str = PURPOSE_CHAT,
        duration_ms: Optional[float] = None,
        ttft_ms: Optional[float] = None,
        input_price: Optional[float] = None,
        output_price: Optional[float] = None,
        cached_tokens: int = 0,
        cached_input_price: Optional[float] = None,
    ):
        """记录一次 AI 调用用量。

        Args:
            purpose: 用途分类，使用 PURPOSE_* 常量
            duration_ms: 完整响应时间（毫秒）
            ttft_ms: 首字符时间（Time To First Token，毫秒）
            cached_tokens: 命中提示词缓存的输入 token（各协议归一化后提供）
            cached_input_price: 缓存 token 单价（元/百万token），留空按折扣估算
        """
        try:
            prompt_tokens = max(0, int(prompt_tokens or 0))
            completion_tokens = max(0, int(completion_tokens or 0))
            cached_tokens = max(0, min(int(cached_tokens or 0), prompt_tokens))
            total_tokens = max(
                0,
                int(
                    total_tokens
                    if total_tokens is not None
                    else prompt_tokens + completion_tokens
                ),
            )
        except (TypeError, ValueError):
            return

        if not total_tokens:
            return

        cost = _estimate_cost(
            model or "default", prompt_tokens, completion_tokens,
            input_price=input_price, output_price=output_price,
            cached_tokens=cached_tokens, cached_input_price=cached_input_price,
        )
        today_str = datetime.now().strftime("%Y-%m-%d")
        current_month = datetime.now().strftime("%Y-%m")
        timestamp = datetime.now().isoformat(timespec="seconds")

        with self._lock:
            stats = self._stats

            # 跨天/跨月自动重置
            last_date = stats["history"][-1]["date"] if stats["history"] else ""
            if last_date and last_date != today_str:
                stats["today"] = 0
            if last_date and last_date[:7] != current_month:
                stats["month"] = 0
            # 顶层聚合
            stats["today"] = stats.get("today", 0) + total_tokens
            stats["month"] = stats.get("month", 0) + total_tokens
            stats["total"] = stats.get("total", 0) + total_tokens
            stats["estimated_cost"] = float(stats.get("estimated_cost", 0) or 0) + cost

            # 每日历史（查找或创建今天的条目）
            today_entry = None
            for entry in stats["history"]:
                if entry.get("date") == today_str:
                    today_entry = entry
                    break
            if today_entry:
                today_entry["input"] += prompt_tokens
                today_entry["output"] += completion_tokens
                today_entry["total"] += total_tokens
                today_entry["cache"] = today_entry.get("cache", 0) + cached_tokens
                today_entry["message_count"] = today_entry.get("message_count", 0) + 1
                today_entry["cost"] = (float(today_entry.get("cost", 0) or 0) + cost)
            else:
                stats["history"].append({
                    "date": today_str,
                    "input": prompt_tokens,
                    "output": completion_tokens,
                    "total": total_tokens,
                    "cache": cached_tokens,
                    "message_count": 1,
                    "cost": cost,
                })

            # 限制历史记录保留 90 天
            if len(stats["history"]) > 90:
                stats["history"] = sorted(stats["history"], key=lambda x: x["date"])[-90:]

            # 会话维度
            if session_id:
                sessions = stats.setdefault("sessions", {})
                if session_id not in sessions:
                    sessions[session_id] = {"input": 0, "output": 0, "total": 0, "cache": 0,
                                             "type": channel_type, "message_count": 0}
                s = sessions[session_id]
                s["input"] += prompt_tokens
                s["output"] += completion_tokens
                s["total"] += total_tokens
                s["cache"] = s.get("cache", 0) + cached_tokens
                s["message_count"] = s.get("message_count", 0) + 1

            # 模型维度
            if model:
                models = stats.setdefault("models", {})
                if model not in models:
                    models[model] = {"input": 0, "output": 0, "total": 0, "cache": 0,
                                     "message_count": 0, "cost": 0.0}
                m = models[model]
                m["input"] += prompt_tokens
                m["output"] += completion_tokens
                m["total"] += total_tokens
                m["cache"] = m.get("cache", 0) + cached_tokens
                m["message_count"] = m.get("message_count", 0) + 1
                m["cost"] = float(m.get("cost", 0) or 0) + cost

            # 用户维度
            if user_id:
                users = stats.setdefault("users", {})
                uid = str(user_id)
                if uid not in users:
                    users[uid] = {"input": 0, "output": 0, "total": 0, "cache": 0,
                                  "message_count": 0}
                u = users[uid]
                u["input"] += prompt_tokens
                u["output"] += completion_tokens
                u["total"] += total_tokens
                u["cache"] = u.get("cache", 0) + cached_tokens
                u["message_count"] = u.get("message_count", 0) + 1

            record = {
                "id": str(uuid.uuid4()),
                "timestamp": timestamp,
                "date": today_str,
                "model": model or "unknown",
                "session_id": session_id,
                "channel_type": channel_type,
                "user_id": str(user_id or ""),
                "input": prompt_tokens,
                "output": completion_tokens,
                "total": total_tokens,
                "cached_tokens": cached_tokens,
                "cost": round(cost, 6),
                "source": source or channel_type or "api",
                "purpose": purpose or PURPOSE_CHAT,
            }
            if duration_ms is not None:
                try:
                    record["duration_ms"] = max(0.0, float(duration_ms))
                except (TypeError, ValueError):
                    pass
            if ttft_ms is not None:
                try:
                    record["ttft_ms"] = max(0.0, float(ttft_ms))
                except (TypeError, ValueError):
                    pass

            records = stats.setdefault("records", [])
            records.append(record)
            # 按日期保留 365 天明细，避免长日期范围导出时早期记录被裁掉
            cutoff_date = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
            records[:] = [r for r in records if (r.get("date") or "") >= cutoff_date]
            # 数量上限兜底（防止异常累积导致文件过大）
            if len(records) > 50000:
                del records[: len(records) - 50000]
            stats["records"] = records

            self._save()

        # 成就系统：记录累计 token / 消费 / 对话消息
        try:
            from nbot.achievements.manager import get_achievement_manager
            mgr = get_achievement_manager(self._data_dir)
            mgr.record("tokens", total_tokens)
            mgr.record("cost", cost)
            if (purpose or PURPOSE_CHAT) == PURPOSE_CHAT:
                mgr.record("messages", 1)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 查询统计
    # ------------------------------------------------------------------

    def get_stats(
        self,
        date_range: str = "today",
        *,
        start_date: str = "",
        end_date: str = "",
    ) -> Dict[str, Any]:
        """返回指定时间范围的统计数据。"""
        with self._lock:
            stats = self._stats
            history = list(stats.get("history", []))
            records = list(stats.get("records", []))
            sessions = dict(stats.get("sessions", {}))
            models = dict(stats.get("models", {}))
            users = dict(stats.get("users", {}))

        today_str = datetime.now().strftime("%Y-%m-%d")
        has_custom_range = bool(start_date or end_date)
        records = self._filter_records(records, date_range, start_date, end_date)
        recent_records = sorted(
            records,
            key=lambda item: item.get("timestamp", ""),
            reverse=True,
        )[:100]

        if has_custom_range:
            start = start_date or "0000-00-00"
            end = end_date or "9999-99-99"
            history = [
                h for h in history
                if start <= (h.get("date") or "") <= end
            ]
        elif date_range == "today":
            history = [h for h in history if h.get("date") == today_str]
            # 如果今日无历史记录，使用 today 计数器兜底
            if not history:
                today_val = stats.get("today", 0)
                return {
                    "today": today_val,
                    "month": stats.get("month", 0),
                    "total": stats.get("total", 0),
                    "total_tokens": today_val,
                    "today_input": 0,
                    "today_output": 0,
                    "cached_tokens": 0,
                    "cache_hit_rate": 0,
                    "message_count": 0,
                    "avg_tokens_per_msg": 0,
                    "estimated_cost": f"{float(stats.get('estimated_cost', 0) or 0):.2f}",
                    "active_sessions": len(stats.get("sessions", {})),
                    "avg_response_time": "0",
                    "avg_ttft": "0",
                    "avg_duration": "0",
                    "history": [],
                    "recent_records": recent_records,
                    "records": records,
                    "range_start": today_str,
                    "range_end": today_str,
                    "sessions": sessions,
                    "models": models,
                    "users": users,
                }
        elif date_range == "7d":
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            history = [h for h in history if h.get("date", "") >= cutoff]
        elif date_range == "30d":
            cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            history = [h for h in history if h.get("date", "") >= cutoff]

        # 汇总
        range_dates = [
            item.get("date")
            for item in [*history, *records]
            if item.get("date")
        ]
        range_start = min(range_dates) if range_dates else (start_date or today_str)
        range_end = max(range_dates) if range_dates else (end_date or today_str)

        total_input = sum(h.get("input", 0) for h in history)
        total_output = sum(h.get("output", 0) for h in history)
        total_cached = sum(h.get("cache", 0) for h in history)
        total_tokens = sum(h.get("total", h.get("input", 0) + h.get("output", 0)) for h in history)
        message_count = sum(h.get("message_count", 0) for h in history)
        total_cost = sum(float(h.get("cost", 0) or 0) for h in history)
        durations = []
        ttfts = []
        for record in records:
            try:
                duration = float(record.get("duration_ms", 0) or 0)
            except (TypeError, ValueError):
                continue
            if duration > 0:
                durations.append(duration)
            try:
                ttft = float(record.get("ttft_ms", 0) or 0)
            except (TypeError, ValueError):
                continue
            if ttft > 0:
                ttfts.append(ttft)
        avg_response_time = (
            f"{(sum(durations) / len(durations) / 1000):.1f}"
            if durations
            else "0"
        )
        avg_ttft = (
            f"{(sum(ttfts) / len(ttfts) / 1000):.2f}"
            if ttfts
            else "0"
        )
        avg_duration = (
            f"{(sum(durations) / len(durations) / 1000):.2f}"
            if durations
            else "0"
        )
        active_session_ids = {
            r.get("session_id")
            for r in records
            if r.get("session_id")
        }
        active_sessions = len(active_session_ids) if active_session_ids else len(sessions)

        # 用途维度统计
        purposes: Dict[str, Dict[str, Any]] = {}
        for record in records:
            p = record.get("purpose", PURPOSE_CHAT)
            if p not in purposes:
                purposes[p] = {"input": 0, "output": 0, "total": 0, "message_count": 0, "cost": 0.0}
            purposes[p]["input"] += record.get("input", 0)
            purposes[p]["output"] += record.get("output", 0)
            purposes[p]["total"] += record.get("total", 0)
            purposes[p]["message_count"] += 1
            purposes[p]["cost"] += float(record.get("cost", 0) or 0)

        return {
            "today": stats.get("today", 0),
            "month": stats.get("month", 0),
            "total": stats.get("total", 0),
            "total_tokens": total_tokens,
            "today_input": total_input,
            "today_output": total_output,
            "cached_tokens": total_cached,
            "cache_hit_rate": (
                round(total_cached / total_input * 100, 1) if total_input else 0
            ),
            "message_count": message_count,
            "avg_tokens_per_msg": round(total_tokens / message_count) if message_count > 0 else 0,
            "estimated_cost": f"{total_cost:.2f}",
            "active_sessions": active_sessions,
            "avg_response_time": avg_response_time,
            "avg_ttft": avg_ttft,
            "avg_duration": avg_duration,
            "history": history,
            "recent_records": recent_records,
            "records": records,
            "range_start": range_start,
            "range_end": range_end,
            "sessions": sessions,
            "models": models,
            "users": users,
            "purposes": purposes,
        }

    # ------------------------------------------------------------------
    # 排行榜
    # ------------------------------------------------------------------

    def get_rankings(
        self,
        limit: int = 10,
        *,
        is_session_active: Optional[Callable[[str], bool]] = None,
    ) -> Dict[str, List[Dict]]:
        """返回会话 / 模型 / 用户 / 用途排行榜。

        Args:
            is_session_active: 可选回调，用于判断会话是否仍然存在。
                传入后会话排行榜将排除已删除（回调返回 False）的会话。
        """
        with self._lock:
            sessions = dict(self._stats.get("sessions", {}))
            models = dict(self._stats.get("models", {}))
            users = dict(self._stats.get("users", {}))
            records = list(self._stats.get("records", []))

        # 过滤已删除的会话
        if is_session_active is not None:
            sessions = {
                sid: sdata for sid, sdata in sessions.items()
                if is_session_active(sid)
            }

        def _build(items: Dict, name_key: str = None) -> List[Dict]:
            result = []
            for key, val in items.items():
                name = key if name_key is None else val.get(name_key, key)
                entry = {
                    "name": str(name)[:32],
                    "value": val.get("total", 0) if isinstance(val, dict) else val,
                }
                if isinstance(val, dict):
                    entry.update({
                        "input": val.get("input", 0),
                        "output": val.get("output", 0),
                        "message_count": val.get("message_count", 0),
                        "cost": float(val.get("cost", 0) or 0),
                        "type": val.get("type", ""),
                    })
                result.append(entry)
            result.sort(key=lambda x: x["value"], reverse=True)
            return result[:limit]

        # 用途维度统计
        purposes: Dict[str, Dict[str, Any]] = {}
        for record in records:
            p = record.get("purpose", PURPOSE_CHAT)
            if p not in purposes:
                purposes[p] = {"input": 0, "output": 0, "total": 0, "message_count": 0, "cost": 0.0}
            purposes[p]["input"] += record.get("input", 0)
            purposes[p]["output"] += record.get("output", 0)
            purposes[p]["total"] += record.get("total", 0)
            purposes[p]["message_count"] += 1
            purposes[p]["cost"] += float(record.get("cost", 0) or 0)

        # 为用途添加显示名称
        purposes_with_labels = {}
        for key, val in purposes.items():
            purposes_with_labels[key] = {
                **val,
                "label": PURPOSE_LABELS.get(key, key),
            }

        return {
            "sessions": _build(sessions),
            "models": _build(models),
            "users": _build(users),
            "purposes": _build(purposes_with_labels),
        }

    def get_model_usage(self, model_name: str) -> Dict[str, int]:
        """返回指定模型的今日和近7天 token 用量。

        Returns:
            {"today_total": int, "weekly_total": int, "today_input": int,
             "today_output": int, "weekly_input": int, "weekly_output": int,
             "message_count": int, "cost": float}
        """
        today_str = datetime.now().strftime("%Y-%m-%d")
        week_cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")

        with self._lock:
            records = list(self._stats.get("records", []))

        today_input = today_output = week_input = week_output = 0
        msg_count = 0
        cost = 0.0
        for r in records:
            if r.get("model") != model_name:
                continue
            date = r.get("date", "")
            if date < week_cutoff:
                continue
            inp = r.get("input", 0) or 0
            out = r.get("output", 0) or 0
            week_input += inp
            week_output += out
            msg_count += 1
            cost += float(r.get("cost", 0) or 0)
            if date == today_str:
                today_input += inp
                today_output += out

        return {
            "today_total": today_input + today_output,
            "weekly_total": week_input + week_output,
            "today_input": today_input,
            "today_output": today_output,
            "weekly_input": week_input,
            "weekly_output": week_output,
            "message_count": msg_count,
            "cost": round(cost, 4),
        }

    # ------------------------------------------------------------------
    # 重置
    # ------------------------------------------------------------------

    def reset_daily(self):
        """重置今日计数器（跨天后调用）。"""
        with self._lock:
            self._stats["today"] = 0
            self._save()

    def reset_monthly(self):
        """重置本月计数器（跨月后调用）。"""
        with self._lock:
            self._stats["month"] = 0
            self._save()

    @property
    def data(self) -> Dict[str, Any]:
        """返回原始数据副本。"""
        with self._lock:
            return dict(self._stats)


# ------------------------------------------------------------------
# 单例
# ------------------------------------------------------------------

_instance: Optional[TokenStatsManager] = None


def init_token_stats_manager(data_dir: str) -> TokenStatsManager:
    """初始化单例（服务启动时调用）。"""
    global _instance
    _instance = TokenStatsManager(data_dir)
    return _instance


def get_token_stats_manager() -> TokenStatsManager:
    """获取单例（已在别处初始化）。"""
    global _instance
    if _instance is None:
        raise RuntimeError("TokenStatsManager 未初始化，请先调用 init_token_stats_manager()")
    return _instance
