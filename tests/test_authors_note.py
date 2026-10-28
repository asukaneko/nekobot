"""Author's Note（作者附注）测试：配置归一化 + 管道深度插入"""

from datetime import datetime
from types import SimpleNamespace

import pytest

from nbot.core import authors_note as an
from nbot.core.ai_pipeline import AIPipeline


def test_normalize_accepts_string_and_dict():
    assert an.normalize_authors_note("  保持第一人称  ") == {
        "enabled": True,
        "text": "保持第一人称",
        "depth": 4,
        "role": "system",
        "position": "before_an",
    }

    note = an.normalize_authors_note(
        {"text": "提醒", "depth": 2, "role": "user", "position": "after_an"}
    )
    assert note["depth"] == 2
    assert note["role"] == "user"
    assert note["position"] == "after_an"


def test_normalize_rejects_empty_and_clamps():
    assert an.normalize_authors_note({}) == {}
    assert an.normalize_authors_note({"text": "   "}) == {}
    assert an.normalize_authors_note(None) == {}

    deep = an.normalize_authors_note({"text": "x", "depth": 9999})
    assert deep["depth"] == an.MAX_DEPTH

    weird = an.normalize_authors_note({"text": "x", "depth": "abc", "role": "hacker"})
    assert weird["depth"] == an.DEFAULT_DEPTH
    assert weird["role"] == "system"

    long_note = an.normalize_authors_note({"text": "长" * 5000})
    assert len(long_note["text"]) <= 2000


def test_resolve_prefers_session_then_default():
    assert an.resolve_authors_note({"text": "会话"}, {"text": "全局"})["text"] == "会话"
    assert an.resolve_authors_note({}, {"text": "全局"})["text"] == "全局"
    assert an.resolve_authors_note(None, None) == {}


def test_build_insertion_and_expander():
    assert an.build_insertion({"text": "x", "enabled": False}) is None
    assert an.build_insertion({}) is None

    depth, role, content = an.build_insertion(
        {"text": "{{char}}在这里", "depth": 3, "role": "user"},
        expander=lambda text: text.replace("{{char}}", "小春"),
    )
    assert (depth, role, content) == (3, "user", "小春在这里")


def test_insertion_index_never_before_system():
    assert an.insertion_index(10, 0) == 10
    assert an.insertion_index(10, 4) == 6
    assert an.insertion_index(2, 40) == 1


class _FakeRequest:
    def __init__(self, conversation_id="sess-1", user_id="阿明", sender="阿明"):
        self.conversation_id = conversation_id
        self.user_id = user_id
        self.sender = sender
        self.parent_message_id = None


@pytest.fixture()
def pipeline():
    return AIPipeline()


def _ctx(note, char_name="小春"):
    request = _FakeRequest()
    return SimpleNamespace(
        metadata={
            "authors_note": note,
            "macro_user_name": "阿明",
            "macro_char_name": char_name,
        },
        chat_request=request,
        character_turn=None,
        prompt_stack=None,
        mode_policy=None,
    )


def _messages(count=6):
    return [{"role": "system", "content": "system"}] + [
        {"role": "user" if i % 2 else "assistant", "content": f"m{i}"}
        for i in range(1, count)
    ]


def test_pipeline_inserts_note_at_depth(pipeline):
    messages = _messages(6)
    ctx = _ctx({"text": "{{char}}请更主动些", "depth": 2, "role": "user", "enabled": True})

    result = pipeline._apply_authors_note(ctx, messages)

    assert len(result) == len(messages) + 1
    inserted = result[len(messages) - 2]
    assert inserted["role"] == "user"
    assert inserted["content"] == "小春请更主动些"
    # 原消息顺序不变
    assert [m["content"] for m in result if m["content"] != "小春请更主动些"] == [
        m["content"] for m in messages
    ]
    debug = ctx.metadata.get("prompt_stack_debug") or []
    assert debug and debug[0]["key"] == "authors_note@2"


def test_pipeline_skips_disabled_or_empty_note(pipeline):
    messages = _messages(4)
    assert pipeline._apply_authors_note(_ctx({"text": "", "enabled": True}), messages) is messages
    assert pipeline._apply_authors_note(
        _ctx({"text": "x", "enabled": False}), messages
    ) is messages


def test_pipeline_rule_time_macro(pipeline):
    ctx = _ctx({"text": "现在是 {{time}}", "depth": 0, "role": "system"}, char_name="")
    ctx.metadata["_now"] = datetime(2026, 10, 25, 9, 5)
    result = pipeline._apply_authors_note(ctx, _messages(3))
    inserted = result[-1]
    assert inserted["role"] == "system"
    # 时间宏由系统本地时间填充，只校验格式
    assert inserted["content"].startswith("现在是 ")
    assert len(inserted["content"].split(":")[0]) > 0
