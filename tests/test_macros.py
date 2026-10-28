"""提示词宏系统测试"""

from datetime import datetime

from nbot.core import macros


def _ctx(**kwargs):
    kwargs.setdefault("user_name", "阿明")
    kwargs.setdefault("char_name", "小春")
    kwargs.setdefault("now", datetime(2026, 10, 25, 9, 5, 30))
    return macros.build_context(**kwargs)


def test_names_and_time_macros():
    ctx = _ctx()
    text = "{{char}}对{{user}}说：现在是{{time}}，{{date}}，{{weekday}}。"
    assert macros.expand(text, ctx) == "小春对阿明说：现在是09:05，2026-10-25，星期日。"


def test_datetime_and_unix():
    ctx = _ctx()
    out = macros.expand("{{datetime}}|{{unix}}", ctx)
    assert out.startswith("2026-10-25 09:05|")
    assert out.split("|")[1].isdigit()


def test_missing_names_keep_macro():
    ctx = _ctx(user_name="", char_name="")
    assert macros.expand("{{user}}/{{char}}", ctx) == "{{user}}/{{char}}"


def test_random_and_case_preservation():
    ctx = _ctx()
    for _ in range(20):
        value = macros.expand("{{random::Hello::World::Admin}}", ctx)
        assert value in {"Hello", "World", "Admin"}
    assert macros.expand("{{random:甲,乙}}", ctx) in {"甲", "乙"}


def test_pick_is_stable_for_same_seed():
    ctx_a = _ctx(seed="session-1")
    ctx_b = _ctx(seed="session-1")
    first = macros.expand("{{pick::one::two::three}}", ctx_a)
    second = macros.expand("{{pick::one::two::three}}", ctx_b)
    assert first == second
    assert first in {"one", "two", "three"}


def test_roll_dice_bounds():
    ctx = _ctx()
    for _ in range(30):
        value = int(macros.expand("{{roll::2d6+1}}", ctx))
        assert 3 <= value <= 13
    assert macros.expand("{{roll::d1}}", ctx) == "1"


def test_variables_roundtrip():
    variables = {}
    ctx = _ctx(variables=variables)

    out = macros.expand("{{setvar::天气::暴雨}}今天{{getvar::天气}}", ctx)
    assert out == "今天暴雨"
    assert variables["天气"] == "暴雨"

    assert macros.expand("{{addvar::好感::5}}", ctx) == ""
    assert macros.expand("{{addvar::好感::2.5}}", ctx) == ""
    assert variables["好感"] == 7.5

    macros.expand("{{delvar::天气}}", ctx)
    assert "天气" not in variables
    assert macros.expand("{{getvar::不存在}}", ctx) == ""


def test_variables_are_usable_in_nested_round():
    variables = {"名字": "小春"}
    ctx = _ctx(variables=variables)
    assert macros.expand("{{getvar::名字}}的{{user}}", ctx) == "小春的阿明"


def test_comment_newline_and_unknown():
    ctx = _ctx()
    assert macros.expand("A{{// 这段不要}}B", ctx) == "AB"
    assert macros.expand("A{{newline}}B", ctx) == "A\nB"
    assert macros.expand("{{unknown_macro}}", ctx) == "{{unknown_macro}}"
    assert macros.expand("{{}}", ctx) == ""


def test_trim_and_no_macro_passthrough():
    ctx = _ctx()
    assert macros.expand("  {{trim}}文字  ", ctx) == "文字"
    assert macros.expand("没有宏的文本", ctx) == "没有宏的文本"
    assert macros.expand("", ctx) == ""


def test_helpers():
    assert macros.has_macros("a {{user}}")
    assert not macros.has_macros("plain")
    assert macros.strip_macros("{{char}}你好") == "你好"
    assert macros.strip_macros("") == ""


def test_variable_store_roundtrip(tmp_path):
    store = macros.MacroVariableStore(str(tmp_path))
    assert store.get("s1") == {}

    store.save("s1", {"天气": "暴雨", "好感": 7})
    assert store.get("s1") == {"天气": "暴雨", "好感": 7}
    assert store.get("s2") == {}

    store.save("s1", {})
    assert store.get("s1") == {}
    assert store.clear("s1") is False


def test_variable_store_get_returns_copy(tmp_path):
    store = macros.MacroVariableStore(str(tmp_path))
    store.save("s1", {"a": 1})
    data = store.get("s1")
    data["a"] = 99
    assert store.get("s1") == {"a": 1}


def test_session_macro_helper_uses_names():
    from nbot.web.routes.sessions import _expand_session_macros

    assert _expand_session_macros("{{char}}对{{user}}说你好", "阿明", "小春") == "小春对阿明说你好"
    assert _expand_session_macros("", "阿明", "小春") == ""
