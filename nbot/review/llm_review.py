"""
LLM 驱动 Review Pipeline

调用 chat 模型对一轮对话进行结构化复盘，输出与规则版一致的 ReviewOutput。
模型调用失败或 JSON 解析失败时回退到规则版，保证管线可用性。
"""

from __future__ import annotations

import json
import logging
import re

from nbot.review.models import (
    MemoryItem,
    PlotUpdate,
    RelationshipDelta,
    ReviewInput,
    ReviewOutput,
    ReviewScore,
    WorldBookUpdate,
)

_log = logging.getLogger(__name__)

_REVIEW_SYSTEM_PROMPT = (
    "你是一个专业的 AI 角色扮演复盘引擎。根据给定的对话与上下文，"
    "输出一份结构化 JSON 复盘结果。只输出 JSON，不要输出任何其他文字。"
)


def _build_user_prompt(inp: ReviewInput) -> str:
    context = {
        "conversation_id": inp.conversation_id,
        "character_id": inp.character_id,
        "user_message": inp.user_message,
        "assistant_message": inp.assistant_message,
        "recent_messages": inp.recent_messages[-12:],
        "active_plot_node": inp.active_plot_node,
        "selected_choice": inp.selected_choice,
        "relationship_state": inp.relationship_state,
        "character_state": inp.character_state,
        "world_context": inp.world_context,
        "plot_mode": inp.plot_mode,
        "tool_calls": inp.tool_calls,
    }
    return (
        "以下是需要复盘的对话上下文（JSON）：\n"
        + json.dumps(context, ensure_ascii=False, default=str)
        + "\n\n请输出以下 JSON 结构（字段缺失可为 null / 空数组）：\n"
        + '{\n'
        + '  "should_write_memory": bool,\n'
        + '  "memory_items": [{"target": "user|character|world", "mem_type": "short|long|relationship|event", '
        + '"title": str, "content": str, "importance": 0-1, "ttl": "short|long|permanent"}],\n'
        + '  "relationship_delta": {"affection": int, "trust": int, "familiarity": int, "dependency": int, '
        + '"security": int, "jealousy": int, "reason": str} | null,\n'
        + '  "plot_update": {"should_create_node": bool, "level": "normal|important|turning_point|ending", '
        + '"summary": str, "title": str} | null,\n'
        + '  "world_book_update": {"should_update": bool, "reason": str} | null,\n'
        + '  "scores": {"character_fidelity": 0-1, "immersion": 0-1, "relationship_progress": 0-1, '
        + '"story_progress": 0-1, "memory_value": 0-1, "world_consistency": 0-1, "user_engagement": 0-1, "risk": 0-1}\n'
        + '}\n'
        + '判断标准：普通寒暄且无重要信息时 should_write_memory=false 并应设置 skipped。'
    )


def _extract_json(text: str) -> dict | None:
    text = (text or "").strip()
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start:end + 1]
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _clamp_score(value, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return default


def run_llm_review(inp: ReviewInput) -> ReviewOutput:
    """执行 LLM 版复盘；失败时回退到规则版。"""
    from nbot.review.rule_review import run_rule_review

    try:
        text = _call_chat_model(_REVIEW_SYSTEM_PROMPT, _build_user_prompt(inp))
        data = _extract_json(text)
        if data is None:
            _log.warning("[ReviewLLM] 模型未返回有效 JSON，回退规则版")
            return run_rule_review(inp)
    except Exception as exc:
        _log.warning("[ReviewLLM] LLM 复盘失败（%s），回退规则版", exc)
        return run_rule_review(inp)

    output = ReviewOutput(source="llm")
    output.should_write_memory = bool(data.get("should_write_memory", False))
    output.skipped = bool(data.get("skipped", False))

    for item in (data.get("memory_items") or []):
        if not isinstance(item, dict):
            continue
        output.memory_items.append(MemoryItem(
            target=str(item.get("target") or "user"),
            mem_type=str(item.get("mem_type") or "short"),
            title=str(item.get("title") or ""),
            content=str(item.get("content") or ""),
            importance=_clamp_score(item.get("importance")),
            ttl=str(item.get("ttl") or "short"),
        ))
        if item.get("content"):
            output.should_write_memory = True

    rd = data.get("relationship_delta")
    if isinstance(rd, dict):
        output.relationship_delta = RelationshipDelta(
            affection=int(rd.get("affection") or 0),
            trust=int(rd.get("trust") or 0),
            familiarity=int(rd.get("familiarity") or 0),
            dependency=int(rd.get("dependency") or 0),
            security=int(rd.get("security") or 0),
            jealousy=int(rd.get("jealousy") or 0),
            reason=str(rd.get("reason") or ""),
            conversation_id=inp.conversation_id,
            plot_node_id=(inp.selected_choice or {}).get("node_id", ""),
        )

    pu = data.get("plot_update")
    if isinstance(pu, dict) and pu.get("should_create_node"):
        output.plot_update = PlotUpdate(
            should_create_node=True,
            level=str(pu.get("level") or "normal"),
            summary=str(pu.get("summary") or ""),
            title=str(pu.get("title") or ""),
        )

    wb = data.get("world_book_update")
    if isinstance(wb, dict) and wb.get("should_update"):
        output.world_book_update = WorldBookUpdate(
            should_update=True,
            reason=str(wb.get("reason") or ""),
        )

    sc = data.get("scores")
    if isinstance(sc, dict):
        output.scores = ReviewScore(
            character_fidelity=_clamp_score(sc.get("character_fidelity")),
            immersion=_clamp_score(sc.get("immersion")),
            relationship_progress=_clamp_score(sc.get("relationship_progress")),
            story_progress=_clamp_score(sc.get("story_progress")),
            memory_value=_clamp_score(sc.get("memory_value")),
            world_consistency=_clamp_score(sc.get("world_consistency")),
            user_engagement=_clamp_score(sc.get("user_engagement")),
            risk=_clamp_score(sc.get("risk")),
        )

    # 离线剧情推进仍复用规则逻辑（LLM 不参与）
    try:
        from nbot.review.rule_review import build_offline_plot_update
        output.offline_plot_update = build_offline_plot_update(inp)
    except Exception:
        pass

    return output


def _call_chat_model(system_prompt: str, user_prompt: str) -> str:
    """调用 chat 模型队列获取一次非流式回复文本。"""
    import requests

    from nbot.core.failover import get_failover_state
    from nbot.core.model_adapter import response_json_utf8
    from nbot.core.model_proxy import model_proxy_request_kwargs
    from nbot.core.protocols import get_protocol
    from nbot.web.utils.config_loader import get_model_configs_by_purpose

    configs = get_model_configs_by_purpose("chat")
    if not configs:
        raise RuntimeError("未配置 chat 模型，无法执行 LLM 复盘")

    failover = get_failover_state()
    attempted = set()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_error: Exception | None = None

    for _ in range(len(configs)):
        config = failover.select_model(configs, exclude_ids=attempted)
        if config is None:
            break
        model_id = config.get("model_id", "")
        attempted.add(model_id)
        try:
            provider_type = config.get("provider_type", "openai_compatible")
            protocol = get_protocol(provider_type)
            url = protocol.resolve_url(
                config.get("base_url", ""),
                model=config.get("model", ""),
                append_base_url_path=config.get("append_base_url_path", True),
                api_key=config.get("api_key", ""),
            )
            headers = protocol.build_headers(config.get("api_key", ""))
            payload = protocol.build_payload(
                config.get("model", ""),
                messages,
                tools=None,
                stream=False,
                base_url=config.get("base_url", ""),
                provider_type=provider_type,
            )
            resp = requests.post(
                url, json=payload, headers=headers,
                timeout=120,
                **model_proxy_request_kwargs(config.get("proxy_url", "")),
            )
            resp.raise_for_status()
            normalized = protocol.parse_response(
                response_json_utf8(resp),
                model=config.get("model", ""),
                base_url=config.get("base_url", ""),
                provider_type=provider_type,
            ).to_dict()
            failover.record_success(model_id)
            content = normalized.get("content") or ""
            return str(content).strip()
        except Exception as exc:
            failover.record_failure(model_id, None)
            last_error = exc
            continue

    raise last_error or RuntimeError("LLM 复盘：所有 chat 模型均失败")
