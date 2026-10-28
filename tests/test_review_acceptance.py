"""LLM 复盘验收：规则版可用、LLM 版可解析、失败/坏 JSON 一律回退不炸。"""

import json
import os

import pytest
from flask import Flask

from nbot.review import llm_review
from nbot.review.models import ReviewInput
from nbot.review.pipeline import ReviewPipeline
from nbot.web.routes.review import register_review_routes


def _inp(**overrides) -> ReviewInput:
    base = {
        "conversation_id": "conv-1",
        "character_id": "char-1",
        "user_id": "user-1",
        "user_message": "你好",
        "assistant_message": "你好呀～",
    }
    base.update(overrides)
    return ReviewInput(**base)


@pytest.fixture(autouse=True)
def _no_achievement_writes(monkeypatch):
    """复盘会记成就计数，测试里不要写真实 data/ 目录。"""

    class _Recorder:
        def record(self, counter, amount):
            return None

    monkeypatch.setattr(
        "nbot.achievements.manager.get_achievement_manager", lambda *a, **k: _Recorder()
    )


# ---------------------------------------------------------------------------
# 规则版
# ---------------------------------------------------------------------------


def test_rule_mode_returns_rule_source():
    output = ReviewPipeline(mode="rule").run(_inp())
    payload = output.to_dict()
    assert payload["source"] == "rule"
    assert isinstance(payload["scores"], dict)


def test_rule_mode_skips_trivial_chat():
    output = ReviewPipeline(mode="rule").run(_inp(user_message="随便聊聊", assistant_message="嗯嗯"))
    assert output.skipped is True


def test_unknown_mode_falls_back_to_rule():
    pipeline = ReviewPipeline(mode="nonsense")
    assert pipeline._mode == "rule"
    output = pipeline.run(_inp())
    assert output.source == "rule"


# ---------------------------------------------------------------------------
# LLM 版
# ---------------------------------------------------------------------------


LLM_PAYLOAD = {
    "should_write_memory": True,
    "memory_items": [
        {
            "target": "user",
            "mem_type": "long",
            "title": "喜好",
            "content": "用户很喜欢猫",
            "importance": 2,
            "ttl": "long",
        }
    ],
    "relationship_delta": {"affection": 3, "trust": -1, "reason": "聊得开心"},
    "plot_update": {
        "should_create_node": True,
        "level": "important",
        "summary": "一起去看猫",
        "title": "看猫",
    },
    "scores": {"immersion": 1.7, "risk": -1, "memory_value": 0.4},
}


def test_llm_mode_parses_structured_output(monkeypatch):
    monkeypatch.setattr(
        llm_review, "_call_chat_model", lambda system, user: json.dumps(LLM_PAYLOAD)
    )
    output = ReviewPipeline(mode="llm").run(_inp())

    assert output.source == "llm"
    assert output.should_write_memory is True
    assert output.memory_items[0].content == "用户很喜欢猫"
    assert output.memory_items[0].importance == 1.0  # 越界被夹到 1.0
    assert output.relationship_delta.affection == 3
    assert output.relationship_delta.reason == "聊得开心"
    assert output.plot_update.level == "important"
    assert output.scores.immersion == 1.0
    assert output.scores.risk == 0.0
    assert output.scores.memory_value == 0.4


def test_llm_mode_extracts_fenced_json(monkeypatch):
    fenced = "```json\n" + json.dumps({"scores": {"immersion": 0.6}}) + "\n```"
    monkeypatch.setattr(llm_review, "_call_chat_model", lambda system, user: fenced)
    output = ReviewPipeline(mode="llm").run(_inp())
    assert output.source == "llm"
    assert output.scores.immersion == 0.6


def test_llm_call_failure_falls_back_to_rule(monkeypatch):
    def boom(system, user):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm_review, "_call_chat_model", boom)
    output = ReviewPipeline(mode="llm").run(_inp())
    assert output.source == "rule"  # 回退到规则版，不抛异常


def test_llm_bad_json_falls_back_to_rule(monkeypatch):
    monkeypatch.setattr(
        llm_review, "_call_chat_model", lambda system, user: "抱歉，我无法输出结构化结果"
    )
    output = ReviewPipeline(mode="llm").run(_inp())
    assert output.source == "rule"


def test_pipeline_swallows_unexpected_review_error(monkeypatch):
    """review 内部异常要变成 skipped 输出，不能把整轮对话带崩。"""

    def boom(_inp):
        raise RuntimeError("unexpected")

    monkeypatch.setattr("nbot.review.pipeline.run_rule_review", boom)
    output = ReviewPipeline(mode="rule").run(_inp())
    assert output.skipped is True


# ---------------------------------------------------------------------------
# Web 路由
# ---------------------------------------------------------------------------


class _StubServer:
    def __init__(self, data_dir):
        self.data_dir = str(data_dir)
        self.settings = {}
        self.saved = 0

    def _save_data(self, name):
        self.saved += 1
        if name == "settings":
            os.makedirs(self.data_dir, exist_ok=True)
            with open(
                os.path.join(self.data_dir, "settings.json"), "w", encoding="utf-8"
            ) as handle:
                json.dump(self.settings, handle, ensure_ascii=False)


@pytest.fixture()
def review_client(tmp_path, monkeypatch):
    import nbot.review.pipeline as pipeline_mod

    pipeline_mod._review_pipeline = None
    app = Flask(__name__)
    server = _StubServer(tmp_path)
    register_review_routes(app, server)
    yield app.test_client(), server
    pipeline_mod._review_pipeline = None


def test_review_settings_roundtrip(review_client):
    client, server = review_client
    assert client.get("/api/review/settings").get_json()["mode"] == "rule"

    saved = client.post("/api/review/settings", json={"mode": "llm"})
    assert saved.status_code == 200
    assert saved.get_json() == {"ok": True, "mode": "llm"}
    assert server.settings["review"]["mode"] == "llm"
    assert client.get("/api/review/settings").get_json()["mode"] == "llm"

    assert client.post("/api/review/settings", json={"mode": "bogus"}).status_code == 400


def test_review_run_route_rule_mode(review_client):
    client, _server = review_client
    payload = client.post(
        "/api/review/run",
        json={
            "mode": "rule",
            "conversation_id": "conv-x",
            "user_message": "你好",
            "assistant_message": "嗨",
        },
    )
    assert payload.status_code == 200
    assert payload.get_json()["source"] == "rule"


def test_review_run_route_llm_failure_still_returns(review_client, monkeypatch):
    client, _server = review_client

    def boom(system, user):
        raise RuntimeError("llm down")

    monkeypatch.setattr(llm_review, "_call_chat_model", boom)
    payload = client.post(
        "/api/review/run",
        json={"mode": "llm", "user_message": "你好", "assistant_message": "嗨"},
    )
    assert payload.status_code == 200
    assert payload.get_json()["source"] == "rule"
