"""用户人设（persona）全通道注入测试。

覆盖：persona_id 命中、未绑定回退默认、失效 id 回退、存储异常降级、
AgentService 请求级注入，以及管道 system prompt 兜底注入。
"""

import json
import sys
import types

import pytest

from nbot.core import agent_service, personas
from nbot.core.ai_pipeline import AIPipeline
from nbot.core.chat_models import ChatRequest


PERSONAS = {
    "personas": [
        {"id": "p1", "name": "小林", "pronouns": "", "description": "温和的工程师"},
        {"id": "p2", "name": "阿岚", "pronouns": "他", "description": "爱冒险的旅人"},
    ],
    "default_id": "p1",
}


@pytest.fixture()
def persona_store(tmp_path, monkeypatch):
    target = tmp_path / "data" / "web"
    target.mkdir(parents=True)
    (target / "personas.json").write_text(
        json.dumps(PERSONAS, ensure_ascii=False), encoding="utf-8"
    )
    store = personas.PersonaStore(base_dir=str(tmp_path))
    monkeypatch.setattr(personas, "_STORE", store)
    return store


@pytest.fixture()
def fake_web(monkeypatch):
    """用轻量替身替换 nbot.web.server，避免核心测试拉起 Flask。"""

    def _install(sessions):
        class _Store:
            def get_session(self, session_id):
                return sessions.get(session_id)

        server = types.SimpleNamespace(session_store=_Store())
        module = types.SimpleNamespace(
            WebChatServer=types.SimpleNamespace(
                get_instance=staticmethod(lambda: server)
            )
        )
        monkeypatch.setitem(sys.modules, "nbot.web.server", module)
        return sessions

    _install({})
    return _install


def _request(conversation_id="qq_private_1"):
    return ChatRequest(channel="qq", conversation_id=conversation_id, content="hi")


def test_persona_hit_by_session_binding(persona_store, fake_web):
    fake_web({"qq_private_1": {"persona_id": "p2"}})

    persona = agent_service.resolve_user_persona(_request())

    assert persona["id"] == "p2"
    assert persona["name"] == "阿岚"


def test_unbound_session_falls_back_to_default(persona_store, fake_web):
    fake_web({"qq_private_1": {}})

    persona = agent_service.resolve_user_persona(_request())

    assert persona["id"] == "p1"


def test_invalid_persona_id_falls_back_to_default(persona_store, fake_web):
    fake_web({"qq_private_1": {"persona_id": "does-not-exist"}})

    persona = agent_service.resolve_user_persona(_request())

    assert persona["id"] == "p1"


def test_no_session_record_returns_none(persona_store, fake_web):
    fake_web({})

    assert agent_service.resolve_user_persona(_request()) is None
    assert agent_service.resolve_user_persona(_request("")) is None


def test_store_failure_degrades_without_raising(persona_store, fake_web, monkeypatch):
    fake_web({"qq_private_1": {"persona_id": "p2"}})

    def _boom(session, store=None):
        raise RuntimeError("storage down")

    monkeypatch.setattr(personas, "session_persona", _boom)

    assert agent_service.resolve_user_persona(_request()) is None


def test_apply_metadata_sets_persona_and_macro_name(persona_store, fake_web):
    fake_web({"qq_private_1": {"persona_id": "p1"}})
    metadata = {}

    applied = agent_service.apply_user_persona_metadata(_request(), metadata)

    assert applied is True
    assert metadata["user_persona"]["id"] == "p1"
    assert metadata["macro_user_name"] == "小林"


def test_apply_metadata_keeps_existing_macro_user_name(persona_store, fake_web):
    fake_web({"qq_private_1": {"persona_id": "p1"}})
    metadata = {"macro_user_name": "已有名称"}

    agent_service.apply_user_persona_metadata(_request(), metadata)

    assert metadata["macro_user_name"] == "已有名称"


def test_apply_metadata_is_noop_without_persona(tmp_path, monkeypatch, fake_web):
    target = tmp_path / "data" / "web"
    target.mkdir(parents=True)
    (target / "personas.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        personas, "_STORE", personas.PersonaStore(base_dir=str(tmp_path))
    )
    fake_web({"qq_private_1": {}})
    metadata = {}

    applied = agent_service.apply_user_persona_metadata(_request(), metadata)

    assert applied is False
    assert metadata == {}


def test_agent_service_process_injects_persona(persona_store, fake_web):
    fake_web({"qq_private_1": {"persona_id": "p2"}})
    service = agent_service.AgentService()
    captured = {}

    def _handler(chat_request, **kwargs):
        captured.update(chat_request.metadata or {})
        return "ok"

    service.register_handler("qq", _handler)
    response = service.process(_request())

    assert response.final_content == "ok"
    assert captured["user_persona"]["id"] == "p2"
    assert captured["macro_user_name"] == "阿岚"


def test_pipeline_appends_persona_block(persona_store, fake_web):
    fake_web({"qq_private_1": {"persona_id": "p2"}})
    pipeline = AIPipeline()
    ctx = types.SimpleNamespace(metadata={}, chat_request=_request())
    messages = [{"role": "system", "content": "基础设定"}]

    result = pipeline._apply_user_persona(ctx, messages)

    system = result[0]["content"]
    assert "基础设定" in system
    assert personas.PROMPT_HEADER in system
    assert "阿岚" in system
    assert ctx.metadata["user_persona"]["id"] == "p2"
    assert ctx.metadata["prompt_stack_debug"][0]["key"] == "user_persona"


def test_pipeline_leaves_messages_untouched_without_persona(
    tmp_path, monkeypatch, fake_web
):
    target = tmp_path / "data" / "web"
    target.mkdir(parents=True)
    (target / "personas.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        personas, "_STORE", personas.PersonaStore(base_dir=str(tmp_path))
    )
    fake_web({"qq_private_1": {}})
    pipeline = AIPipeline()
    ctx = types.SimpleNamespace(metadata={}, chat_request=_request())
    messages = [{"role": "system", "content": "基础设定"}]

    result = pipeline._apply_user_persona(ctx, messages)

    assert result == messages
    assert "user_persona" not in ctx.metadata


def test_pipeline_respects_channel_injected_persona(persona_store, fake_web):
    fake_web({})
    pipeline = AIPipeline()
    persona = {"id": "p9", "name": "通道指定", "pronouns": "", "description": "来自聊天服务"}
    ctx = types.SimpleNamespace(
        metadata={"user_persona": persona}, chat_request=_request()
    )

    result = pipeline._apply_user_persona(ctx, [{"role": "system", "content": "x"}])

    assert "通道指定" in result[0]["content"]
    assert ctx.metadata["user_persona"]["id"] == "p9"
