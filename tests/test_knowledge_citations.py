"""知识库 citations 进聊天：文本 + 引用元数据 + Web 响应字段测试。"""

import types

import pytest

from nbot.core.ai_pipeline import AIPipeline, NoOpProgressReporter, PipelineContext
from nbot.core.chat_models import ChatRequest
from nbot.services import chat_service


class FakeDoc:
    def __init__(self, title, doc_id="doc-1"):
        self.id = doc_id
        self.title = title


def _citation(doc_id, title, score=0.8, chunk_index=0):
    return {
        "id": "K1",
        "doc_id": doc_id,
        "title": title,
        "source": "upload",
        "base_id": "default",
        "chunk_index": chunk_index,
        "start_char": 0,
        "end_char": 42,
        "chars": 42,
        "snippet": f"{title} 的片段",
        "score": score,
        "similarity": score,
        "rerank_score": None,
        "tags": ["tag-a"],
    }


class FakeKnowledgeManager:
    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.queries = []

    def search_with_citations(self, query, base_id=None, top_k=3, **kwargs):
        self.queries.append(query)
        if self.error:
            raise self.error
        return self.payload


@pytest.fixture()
def knowledge(monkeypatch):
    def _install(payload=None, error=None):
        manager = FakeKnowledgeManager(payload=payload, error=error)
        monkeypatch.setattr(chat_service, "KNOWLEDGE_AVAILABLE", True)
        monkeypatch.setattr(chat_service, "get_knowledge_manager", lambda: manager)
        return manager

    return _install


def test_citations_carried_with_text(knowledge):
    payload = {
        "query": "怎么部署",
        "results": [(FakeDoc("部署手册"), 0.82, "先装依赖再启动"), (FakeDoc("故障排查"), 0.5, "看日志")],
        "citations": [
            _citation("doc-1", "部署手册", 0.82, 0),
            _citation("doc-2", "故障排查", 0.5, 3),
        ],
    }
    knowledge(payload)

    text, citations = chat_service.search_knowledge_base_with_citations("怎么部署")

    assert "部署手册" in text and "先装依赖再启动" in text
    assert [c["doc_id"] for c in citations] == ["doc-1", "doc-2"]
    assert citations[0]["chunk_index"] == 0
    assert set(citations[0]) >= {
        "doc_id",
        "title",
        "source",
        "chunk_index",
        "start_char",
        "end_char",
        "snippet",
        "score",
        "rerank_score",
        "tags",
    }


def test_empty_knowledge_base_returns_empty(knowledge):
    knowledge({"query": "x", "results": [], "citations": []})

    assert chat_service.search_knowledge_base_with_citations("x") == ("", [])


def test_search_failure_degrades(knowledge):
    knowledge(error=RuntimeError("chroma down"))

    assert chat_service.search_knowledge_base_with_citations("x") == ("", [])


def test_low_similarity_filtered_out(knowledge):
    payload = {
        "query": "x",
        "results": [(FakeDoc("弱相关"), 0.05, "无关内容")],
        "citations": [_citation("doc-9", "弱相关", 0.05)],
    }
    knowledge(payload)

    assert chat_service.search_knowledge_base_with_citations("x") == ("", [])


def test_duplicate_titles_keep_first_citation(knowledge):
    payload = {
        "query": "x",
        "results": [
            (FakeDoc("同一篇"), 0.9, "片段A"),
            (FakeDoc("同一篇"), 0.8, "片段B"),
        ],
        "citations": [
            _citation("doc-1", "同一篇", 0.9, 0),
            _citation("doc-1", "同一篇", 0.8, 1),
        ],
    }
    knowledge(payload)

    text, citations = chat_service.search_knowledge_base_with_citations("x")

    assert len(citations) == 1
    assert "片段B" not in text


def test_legacy_wrapper_still_returns_text_only(knowledge):
    payload = {
        "query": "x",
        "results": [(FakeDoc("部署手册"), 0.9, "正文")],
        "citations": [_citation("doc-1", "部署手册", 0.9)],
    }
    knowledge(payload)

    text = chat_service.search_knowledge_base("x")

    assert isinstance(text, str)
    assert "部署手册" in text


def _ctx():
    return PipelineContext(
        chat_request=ChatRequest(channel="qq", conversation_id="qq_private_1", content="问")
    )


class _CallbacksWithCitations:
    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = 0

    def search_knowledge(self, ctx, query):
        self.calls += 1
        return "回退文本"

    def search_knowledge_with_citations(self, ctx, query):
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _LegacyCallbacks:
    def __init__(self):
        self.calls = 0

    def search_knowledge(self, ctx, query):
        self.calls += 1
        return "旧文本"


def test_pipeline_stores_citations_metadata():
    ctx = _ctx()
    citations = [_citation("doc-1", "部署手册")]
    callbacks = _CallbacksWithCitations(("知识文本", citations))

    AIPipeline()._phase_knowledge(ctx, callbacks, NoOpProgressReporter())

    assert ctx.knowledge_text == "知识文本"
    assert ctx.knowledge_retrieved is True
    assert ctx.metadata["knowledge_citations"] == citations


def test_pipeline_without_citations_keeps_metadata_clean():
    ctx = _ctx()

    AIPipeline()._phase_knowledge(ctx, _LegacyCallbacks(), NoOpProgressReporter())

    assert ctx.knowledge_text == "旧文本"
    assert "knowledge_citations" not in ctx.metadata


def test_pipeline_falls_back_when_citation_search_raises():
    ctx = _ctx()
    callbacks = _CallbacksWithCitations(RuntimeError("boom"))

    AIPipeline()._phase_knowledge(ctx, callbacks, NoOpProgressReporter())

    assert ctx.knowledge_text == "回退文本"
    assert "knowledge_citations" not in ctx.metadata


def test_pipeline_ignores_non_tuple_result():
    ctx = _ctx()
    callbacks = _CallbacksWithCitations("只有文本")

    AIPipeline()._phase_knowledge(ctx, callbacks, NoOpProgressReporter())

    assert ctx.knowledge_text == "只有文本"
    assert "knowledge_citations" not in ctx.metadata


def test_web_callbacks_citations_helper():
    from nbot.web.ai_service import WebCallbacks

    citations = [_citation("doc-1", "部署手册")]
    ctx = types.SimpleNamespace(metadata={"knowledge_citations": citations})

    assert WebCallbacks._knowledge_citations(ctx) == citations
    assert WebCallbacks._knowledge_citations(types.SimpleNamespace(metadata={})) == []
    assert (
        WebCallbacks._knowledge_citations(
            types.SimpleNamespace(metadata={"knowledge_citations": "bad"})
        )
        == []
    )
