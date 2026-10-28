"""上下文占比统计（7 类口径）测试"""

from nbot.web.context_stats import (
    build_context_stats,
    empty_categories,
    estimate_text_tokens,
    message_content,
)


def _session(messages, **extra):
    data = {"messages": messages}
    data.update(extra)
    return data


def _msg(role, content, msg_id="", **extra):
    payload = {"role": role, "content": content}
    if msg_id:
        payload["id"] = msg_id
    payload.update(extra)
    return payload


def test_categories_cover_seven_buckets_and_rows_sum_to_total():
    session = _session(
        [
            _msg("system", "你是小春", "sys-1"),
            _msg("user", "你好", "u1"),
            _msg("assistant", "你好呀", "a1"),
            _msg("system", "压缩摘要内容", "summary_1"),
            _msg("system", "━━ 归档分隔 ━━", "archive_divider_1"),
            _msg("tool", "工具输出", "t1"),
        ]
    )
    payload = build_context_stats(session, max_context_length=8000)

    categories = payload["categories"]
    assert len(categories) == 7
    assert set(categories) == set(empty_categories())

    total = payload["total"]
    assert total["chars"] == sum(c["chars"] for c in categories.values())
    assert total["tokens"] == sum(c["tokens"] for c in categories.values())
    assert total["messages"] == sum(c["messages"] for c in categories.values())

    assert categories["system_prompt"]["messages"] == 1
    assert categories["compression_summary"]["messages"] == 1
    assert categories["other"]["messages"] == 1  # 归档分隔行
    assert categories["user_messages"]["messages"] == 1
    assert categories["assistant_messages"]["messages"] == 1
    assert categories["tool_traces"]["messages"] == 1
    assert payload["max_context_length"] == 8000


def test_message_count_matches_visible_messages():
    messages = [
        _msg("system", "提示词", "s1"),
        _msg("user", "a", "u1"),
        _msg("assistant", "b", "a1"),
        _msg("user", "已删除", "u2", deleted=True),
    ]
    payload = build_context_stats(_session(messages))
    assert payload["total"]["messages"] == 3  # 软删除不计入


def test_tool_call_history_counted_without_message():
    session = _session(
        [
            _msg("system", "s", "s1"),
            _msg(
                "assistant",
                "调用完成",
                "a1",
                tool_call_history=[{"name": "search_files", "arguments": {"pattern": "x"}}],
            ),
        ]
    )
    payload = build_context_stats(session)
    traces = payload["categories"]["tool_traces"]
    assert traces["chars"] > 0
    assert traces["messages"] == 0


def test_agent_session_includes_tool_definitions():
    session = _session([_msg("system", "s", "s1")], session_mode="agent")
    tools = [{"type": "function", "function": {"name": "demo", "description": "示例"}}]

    with_tools = build_context_stats(session, tools=tools)
    assert with_tools["categories"]["tool_definitions"]["chars"] > 0
    assert with_tools["categories"]["tool_definitions"]["tokens"] > 0

    without_tools = build_context_stats(session, tools=[])
    assert without_tools["categories"]["tool_definitions"]["chars"] == 0


def test_non_agent_session_ignores_tools():
    session = _session([_msg("system", "s", "s1")], session_mode="character")
    payload = build_context_stats(session, tools=[{"function": {"name": "demo"}}])
    assert payload["categories"]["tool_definitions"]["chars"] == 0


def test_empty_session_is_zeroed():
    payload = build_context_stats({"messages": []})
    assert payload["total"] == {"chars": 0, "tokens": 0, "messages": 0}
    assert payload["max_context_length"] >= 1


def test_estimator_cjk_and_ascii():
    assert estimate_text_tokens("") == 0
    cjk = estimate_text_tokens("你好世界")
    ascii_tokens = estimate_text_tokens("hello world")
    assert cjk >= 4
    assert ascii_tokens >= 2


def test_message_content_includes_attachment_metadata():
    msg = _msg(
        "user",
        "看这个",
        "u1",
        attachments=[{"name": "a.pdf", "type": "application/pdf", "extracted_text": "PDF 内容"}],
    )
    text = message_content(msg)
    assert "看这个" in text
    assert "a.pdf" in text
    assert "PDF 内容" in text
