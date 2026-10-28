"""知识库检索增强测试：rerank / MMR / citations。"""

from __future__ import annotations

import pytest
from flask import Flask

from nbot.core.knowledge import Document, KnowledgeBase, KnowledgeManager
from nbot.core.knowledge.rerank import (
    RerankService,
    drop_near_duplicates,
    jaccard,
    mmr_select,
    token_bag,
)


def _doc(doc_id: str, title: str, content: str) -> Document:
    return Document(id=doc_id, title=title, content=content, source="test")


class FakeStore:
    def __init__(self, bases, chunks, documents):
        self.bases = bases
        self.chunks = chunks
        self.documents = documents
        self.search_calls = []

    def load_base(self, base_id):
        return self.bases.get(base_id)

    def load_document(self, doc_id):
        return self.documents.get(doc_id)

    def search_chunks(self, base_id, query, top_k=5):
        self.search_calls.append((base_id, query, top_k))
        return self.chunks.get(base_id, [])[:top_k]


def _manager(chunks, documents, base_id="default") -> KnowledgeManager:
    base = KnowledgeBase(id=base_id, name="测试库")
    manager = object.__new__(KnowledgeManager)
    manager.store = FakeStore({base_id: base}, {base_id: chunks}, documents)
    return manager


@pytest.fixture(autouse=True)
def _no_rerank(monkeypatch):
    monkeypatch.setattr("nbot.core.knowledge.get_rerank_service", lambda: None)


# ---------------------------------------------------------------------------
# token / MMR
# ---------------------------------------------------------------------------


def test_token_bag_adds_cjk_bigrams():
    tokens = token_bag("角色卡设定")
    assert "角" in tokens
    assert "角色" in tokens
    assert token_bag("") == set()


def test_jaccard_bounds():
    assert jaccard(set(), {"a"}) == 0.0
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert 0.0 < jaccard({"a", "b"}, {"a", "c"}) < 1.0


def test_mmr_prefers_diverse_chunks():
    items = [
        {"text": "角色卡里写着她是猫耳女仆，喜欢甜食与晒太阳。", "score": 0.95},
        {"text": "角色卡里写着她是猫耳女仆，喜欢甜食与晒太阳呀。", "score": 0.94},
        {"text": "她经营一家深夜咖啡馆，只在雨夜开门。", "score": 0.80},
    ]
    picked = mmr_select(items, 2, lambda_param=0.5)
    assert len(picked) == 2
    assert picked[0]["score"] == 0.95
    # 第二篇应是不同话题，而不是近似重复的 0.94
    assert picked[1]["score"] == 0.80


def test_mmr_returns_all_when_fewer_than_top_k():
    items = [{"text": "a", "score": 0.2}]
    assert mmr_select(items, 3) == items


def test_drop_near_duplicates_keeps_first():
    items = [
        {"text": "重复的片段内容一模一样"},
        {"text": "重复的片段内容一模一样"},
        {"text": "完全不同的另外一段"},
    ]
    kept = drop_near_duplicates(items)
    assert len(kept) == 2
    assert kept[1]["text"] == "完全不同的另外一段"


# ---------------------------------------------------------------------------
# rerank 解析
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]},
        {"data": [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.2}]},
        [{"index": 1, "score": 0.9}, {"index": 0, "score": 0.2}],
    ],
)
def test_rerank_parse_shapes(payload):
    parsed = RerankService._parse(payload, 2)
    assert parsed is not None
    assert parsed[0] == (1, 0.9)
    assert parsed[1] == (0, 0.2)


def test_rerank_parse_ignores_bad_rows():
    assert RerankService._parse({"results": []}, 2) is None
    assert RerankService._parse({"results": [{"index": 9, "score": 0.5}]}, 2) is None
    assert RerankService._parse("nope", 2) is None


def test_rerank_endpoint_and_disabled():
    service = RerankService(base_url="https://api.example.com/v1", model="rerank-x")
    assert service.endpoint == "https://api.example.com/v1/rerank"
    assert service.enabled is True

    direct = RerankService(base_url="https://api.example.com/rerank", model="m")
    assert direct.endpoint == "https://api.example.com/rerank"

    custom = RerankService(base_url="https://api.example.com", model="m", path="readme")
    assert custom.endpoint == "https://api.example.com/readme"

    assert RerankService(base_url="", model="m").enabled is False
    assert RerankService(base_url="https://x", model="m").rerank("q", []) is None


def test_rerank_request_failure_returns_none(monkeypatch):
    service = RerankService(base_url="https://api.example.com", model="m")

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr("requests.post", boom)
    assert service.rerank("query", ["doc"]) is None


def test_rerank_success_maps_scores(monkeypatch):
    service = RerankService(base_url="https://api.example.com", model="m")

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"results": [{"index": 1, "relevance_score": 0.9}]}

    monkeypatch.setattr("requests.post", lambda *a, **k: Response())
    assert service.scores_for("query", ["a", "b"]) == {1: 0.9}


# ---------------------------------------------------------------------------
# retrieve / citations
# ---------------------------------------------------------------------------


def test_retrieve_builds_citations():
    content = "NekoBot 是一个多通道角色扮演系统。" + "它支持 QQ、Web、CLI。" * 10
    doc = _doc("d1", "NekoBot 简介", content)
    manager = _manager(
        chunks=[(content, 0.82, {"doc_id": "d1", "chunk_index": 0})],
        documents={"d1": doc},
    )

    payload = manager.retrieve("NekoBot 是什么", top_k=3)

    assert len(payload["results"]) == 1
    assert payload["results"][0][0].id == "d1"
    citation = payload["citations"][0]
    assert citation["id"] == "K1"
    assert citation["doc_id"] == "d1"
    assert citation["title"] == "NekoBot 简介"
    assert citation["chunk_index"] == 0
    assert citation["start_char"] == 0
    assert citation["end_char"] == len(content)
    assert citation["chars"] == len(content)
    assert citation["score"] == pytest.approx(0.82, abs=1e-4)
    assert citation["similarity"] == pytest.approx(0.82, abs=1e-4)
    assert citation["rerank_score"] is None
    assert payload["meta"]["rerank"]["enabled"] is False
    assert payload["meta"]["bases"] == ["default"]


def test_retrieve_keeps_one_chunk_per_doc_by_default():
    content = "第一段内容" * 40
    doc = _doc("d1", "长文", content)
    manager = _manager(
        chunks=[
            (content[:200], 0.9, {"doc_id": "d1", "chunk_index": 0}),
            (content[200:400], 0.8, {"doc_id": "d1", "chunk_index": 1}),
        ],
        documents={"d1": doc},
    )

    payload = manager.retrieve("内容", top_k=5, mmr=False)
    assert len(payload["results"]) == 1

    payload = manager.retrieve("内容", top_k=5, mmr=False, max_per_doc=2)
    assert len(payload["results"]) == 2
    assert [item["chunk_index"] for item in payload["citations"]] == [0, 1]


def test_retrieve_falls_back_to_chunk_index_offsets():
    doc = _doc("d1", "长文", "x" * 1200)
    manager = _manager(
        chunks=[("不在原文里的片段", 0.5, {"doc_id": "d1", "chunk_index": 2})],
        documents={"d1": doc},
    )
    citation = manager.retrieve("查询", top_k=1)["citations"][0]
    assert citation["start_char"] == 2 * 450
    assert citation["start_char"] >= 0


def test_retrieve_uses_rerank_scores(monkeypatch):
    doc_a = _doc("a", "A", "数据库索引")
    doc_b = _doc("b", "B", "咖啡与猫")
    manager = _manager(
        chunks=[
            ("数据库索引", 0.90, {"doc_id": "a", "chunk_index": 0}),
            ("咖啡与猫", 0.55, {"doc_id": "b", "chunk_index": 0}),
        ],
        documents={"a": doc_a, "b": doc_b},
    )

    class FakeService:
        model = "fake-rerank"
        weight = 1.0

        def scores_for(self, query, documents):
            return {0: 0.10, 1: 0.95}

    monkeypatch.setattr("nbot.core.knowledge.get_rerank_service", lambda: FakeService())
    payload = manager.retrieve("猫", top_k=2, mmr=False)

    assert [citation["doc_id"] for citation in payload["citations"]] == ["b", "a"]
    assert payload["citations"][0]["rerank_score"] == pytest.approx(0.95)
    assert payload["meta"]["rerank"]["applied"] is True
    assert payload["meta"]["rerank"]["model"] == "fake-rerank"


def test_retrieve_rerank_false_skips_service(monkeypatch):
    called = []

    class FakeService:
        model = "fake"
        weight = 1.0

        def scores_for(self, query, documents):
            called.append(query)
            return {0: 1.0}

    monkeypatch.setattr("nbot.core.knowledge.get_rerank_service", lambda: FakeService())
    doc = _doc("a", "A", "内容")
    manager = _manager(
        chunks=[("内容", 0.5, {"doc_id": "a", "chunk_index": 0})],
        documents={"a": doc},
    )
    payload = manager.retrieve("查询", top_k=1, rerank=False)
    assert called == []
    assert payload["meta"]["rerank"]["enabled"] is False


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_knowledge_search_route_returns_citations():
    from nbot.web.routes.knowledge import register_knowledge_routes

    class FakeManager:
        def search_with_citations(self, query, **kwargs):
            return {
                "query": query,
                "results": [(_doc("d1", "标题", "片段内容"), 0.7, "片段内容")],
                "citations": [{
                    "id": "K1",
                    "doc_id": "d1",
                    "title": "标题",
                    "chunk_index": 0,
                    "start_char": 0,
                    "end_char": 4,
                    "snippet": "片段内容",
                    "score": 0.7,
                    "similarity": 0.7,
                    "rerank_score": None,
                }],
                "meta": {"rerank": {"enabled": False}, "mmr": {"enabled": True}},
            }

    class FakeServer:
        KNOWLEDGE_MANAGER_AVAILABLE = True

        def get_knowledge_manager(self):
            return FakeManager()

    app = Flask(__name__)
    register_knowledge_routes(app, FakeServer())
    client = app.test_client()

    response = client.post("/api/knowledge/search", json={"query": "标题"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["citations"][0]["title"] == "标题"
    assert data["results"][0]["doc_id"] == "d1"

    bad = client.post("/api/knowledge/search", json={})
    assert bad.status_code == 400
