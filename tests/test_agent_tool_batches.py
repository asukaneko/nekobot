"""工具循环：只读并发 / 写串行分批，以及轨迹实时回调测试。"""

import threading
import time

from nbot.core.agent_service import AgentHarness, ToolLoopHooks


def _call(call_id, name):
    return {"id": call_id, "name": name, "arguments": {}}


def _make_model_call(tool_calls):
    state = {"n": 0}

    def _model_call(messages, stop_event=None):
        state["n"] += 1
        if state["n"] == 1:
            return {"content": "思考中", "tool_calls": tool_calls}
        return {"content": "完成", "tool_calls": []}

    return _model_call


class _Recorder:
    def __init__(self, delay=0.12):
        self.delay = delay
        self.spans = []
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def executor(self, tool_call, thinking, iteration, messages):
        name = tool_call.get("name", "")
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            start = time.perf_counter()
        time.sleep(self.delay)
        with self.lock:
            self.active -= 1
            end = time.perf_counter()
        self.spans.append((name, start, end))
        return {"success": True, "name": name}


def _overlaps(a, b):
    return a[0] < b[1] and b[0] < a[1]


def _run(tool_calls, classifier):
    recorder = _Recorder()
    harness = AgentHarness(
        initial_messages=[{"role": "user", "content": "hi"}],
        model_call=_make_model_call(tool_calls),
        tool_executor=recorder.executor,
        read_only_classifier=classifier,
    )
    result = harness.run()
    spans = {name: (start, end) for name, start, end in recorder.spans}
    return result, spans, recorder


def test_read_only_calls_run_concurrently():
    calls = [_call("1", "search_web"), _call("2", "read_memory")]
    result, spans, recorder = _run(calls, lambda name, args: True)

    assert result.final_content == "完成"
    assert recorder.max_active == 2
    assert _overlaps(spans["search_web"], spans["read_memory"])


def test_write_tool_runs_serially_after_reads():
    calls = [
        _call("1", "search_web"),
        _call("2", "get_weather"),
        _call("3", "workspace_create_file"),
        _call("4", "read_memory"),
    ]
    readonly = {"search_web", "get_weather", "read_memory"}
    result, spans, recorder = _run(calls, lambda name, args: name in readonly)

    assert result.final_content == "完成"
    assert recorder.max_active == 2  # 两个只读并发，写工具时不与其他重叠
    write_span = spans["workspace_create_file"]
    for name in ("search_web", "get_weather", "read_memory"):
        assert not _overlaps(write_span, spans[name])
    # 写操作之后的只读调用必须晚于写操作开始
    assert spans["read_memory"][0] >= write_span[1]


def test_unknown_tools_default_to_serial():
    calls = [_call("1", "custom_a"), _call("2", "custom_b")]
    result, spans, recorder = _run(calls, lambda name, args: False)

    assert result.final_content == "完成"
    assert recorder.max_active == 1
    assert not _overlaps(spans["custom_a"], spans["custom_b"])


def test_message_order_follows_tool_call_order():
    calls = [
        _call("1", "search_web"),
        _call("2", "read_memory"),
        _call("3", "workspace_create_file"),
    ]
    result, _spans, _recorder = _run(
        calls, lambda name, args: name in {"search_web", "read_memory"}
    )

    tool_messages = [m for m in result.tool_messages if m.get("role") == "tool"]
    assert [m["name"] for m in tool_messages] == [
        "search_web",
        "read_memory",
        "workspace_create_file",
    ]


def test_on_tool_trace_fires_per_step():
    seen = []
    calls = [_call("1", "search_web"), _call("2", "workspace_create_file")]

    harness = AgentHarness(
        initial_messages=[{"role": "user", "content": "hi"}],
        model_call=_make_model_call(calls),
        tool_executor=lambda call, thinking, iteration, messages: {"success": True},
        hooks=ToolLoopHooks(on_tool_trace=lambda trace: seen.append(trace.tool_name)),
        read_only_classifier=lambda name, args: name == "search_web",
    )
    harness.run()

    assert seen == ["search_web", "workspace_create_file"]


def test_parallel_disabled_runs_everything_serially():
    calls = [_call("1", "search_web"), _call("2", "read_memory")]
    recorder = _Recorder()
    harness = AgentHarness(
        initial_messages=[{"role": "user", "content": "hi"}],
        model_call=_make_model_call(calls),
        tool_executor=recorder.executor,
        parallel_tool_execution=False,
        read_only_classifier=lambda name, args: True,
    )
    harness.run()

    assert recorder.max_active == 1
