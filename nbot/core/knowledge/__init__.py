"""
知识库系统 (RAG - Retrieval Augmented Generation)
支持用户创建和管理知识库，使用 ChromaDB 进行向量检索
"""
import os
import json
import hashlib
import logging
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import re

from nbot.core.knowledge.rerank import (
    DEFAULT_MMR_LAMBDA,
    drop_near_duplicates,
    get_rerank_service,
    mmr_select,
)

_log = logging.getLogger(__name__)

# 导入新的配置加载器
try:
    from nbot.web.utils.config_loader import get_embedding_model_config
    NEW_CONFIG_AVAILABLE = True
except ImportError:
    NEW_CONFIG_AVAILABLE = False
    _log.warning("New config loader not available, using fallback")

# 尝试导入 chromadb
try:
    import chromadb
    from chromadb.config import Settings
    CHROMA_AVAILABLE = True
except ImportError:
    chromadb = None
    CHROMA_AVAILABLE = False
    _log.warning("chromadb not installed, knowledge base will use simple fallback")

@dataclass
class Document:
    """文档"""
    id: str
    title: str
    content: str
    source: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class KnowledgeBase:
    """知识库"""
    id: str
    name: str
    description: str = ""
    owner_type: str = "user"  # user 或 group
    owner_id: str = ""
    documents: List[str] = field(default_factory=list)  # 文档ID列表
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


class TextProcessor:
    """文本处理器"""

    @staticmethod
    def chunk_text(
        text: str,
        chunk_size: int = 500,
        overlap: int = 50
    ) -> List[str]:
        """将文本分块"""
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start = 0

        while start < len(text):
            end = start + chunk_size

            if end < len(text):
                # 优先在句号、换行处分块
                for sep in ['\n\n', '\n', '。', '！', '？', '.', '!', '?']:
                    pos = text.rfind(sep, start, end)
                    if pos > start:
                        end = pos + len(sep)
                        break

            chunk = text[start:end].strip()
            if chunk:
                chunks.append(chunk)

            start = end - overlap
            if start < 0:
                start = 0

        return chunks

    @staticmethod
    def clean_text(text: str) -> str:
        """清理文本"""
        text = re.sub(r'\s+', ' ', text)
        text = text.strip()
        return text


class EmbeddingService:
    """Embedding 服务"""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "text-embedding-3-small",
        proxy_url: str = "",
    ):
        self.api_key = api_key
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.proxy_url = proxy_url or ""
        self._cache: Dict[str, List[float]] = {}

    def _get_cache_key(self, text: str) -> str:
        return hashlib.md5(f"{self.model}:{text}".encode()).hexdigest()

    def get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """获取文本的嵌入向量"""
        if not self.api_key or not self.model:
            return self._get_fallback_embeddings(texts)

        results = []
        uncached_texts = []
        uncached_indices = []

        # 检查缓存
        for i, text in enumerate(texts):
            cache_key = self._get_cache_key(text)
            if cache_key in self._cache:
                results.append(self._cache[cache_key])
            else:
                results.append(None)
                uncached_texts.append(text)
                uncached_indices.append(i)

        # 调用 API 获取未缓存的 embedding
        if uncached_texts:
            try:
                new_embeddings = self._call_embedding_api(uncached_texts)
                for j, emb in enumerate(new_embeddings):
                    idx = uncached_indices[j]
                    results[idx] = emb
                    cache_key = self._get_cache_key(uncached_texts[j])
                    self._cache[cache_key] = emb
            except Exception as e:
                _log.error(f"[Embedding] API call failed: {e}")
                # 使用 fallback
                for j, text in enumerate(uncached_texts):
                    idx = uncached_indices[j]
                    fallback = self._get_fallback_embedding(text)
                    results[idx] = fallback

        return results

    def _call_embedding_api(self, texts: List[str]) -> List[List[float]]:
        """调用 OpenAI 兼容的 embedding API"""
        # 构建 embedding 端点
        if "/v1" in self.base_url:
            url = self.base_url.replace("/chat/completions", "").rstrip("/")
            if not url.endswith("/embeddings"):
                url = f"{url}/embeddings"
        else:
            url = f"{self.base_url}/v1/embeddings"

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float"
        }

        import requests
        from nbot.core.model_proxy import model_proxy_request_kwargs

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=30,
            **model_proxy_request_kwargs(self.proxy_url),
        )
        response.raise_for_status()
        data = response.json()

        embeddings = []
        for item in sorted(data.get("data", []), key=lambda x: x.get("index", 0)):
            embeddings.append(item.get("embedding", []))

        # 记录 token 用量（embedding 按 token 计费）
        try:
            from nbot.core.token_stats import get_token_stats_manager, PURPOSE_EMBEDDING
            usage_data = data.get("usage", {})
            if usage_data:
                stats_mgr = get_token_stats_manager()
                stats_mgr.record_usage(
                    prompt_tokens=usage_data.get("prompt_tokens", 0) or 0,
                    completion_tokens=0,  # embedding 没有输出 token
                    total_tokens=usage_data.get("total_tokens", 0) or 0,
                    model=self.model or "",
                    channel_type="embedding",
                    source="embedding",
                    purpose=PURPOSE_EMBEDDING,
                )
        except Exception:
            pass

        return embeddings

    def _get_fallback_embeddings(self, texts: List[str]) -> List[List[float]]:
        """简化的后备 embedding，基于词频"""
        return [self._get_fallback_embedding(text) for text in texts]

    def _get_fallback_embedding(self, text: str) -> List[float]:
        """单个文本的后备 embedding"""
        # 使用简单的哈希向量化
        words = re.findall(r'[\w]+', text.lower())
        vector = [0.0] * 256

        for word in set(words):
            hash_val = int(hashlib.md5(word.encode()).hexdigest()[:8], 16)
            idx = hash_val % 256
            vector[idx] += 1.0

        # 归一化
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]

        return vector


class ChromaKnowledgeStore:
    """基于 ChromaDB 的知识库存储"""

    def __init__(self, base_dir: str = "saved_message/knowledge"):
        self.base_dir = Path(base_dir)
        self.bases_dir = self.base_dir / "bases"
        self.documents_dir = self.base_dir / "documents"

        self.bases_dir.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)

        self._chroma_client = None
        self._embedding_service: Optional[EmbeddingService] = None

    def _get_base_file(self, base_id: str) -> Path:
        return self.bases_dir / f"{base_id}.json"

    def _get_doc_file(self, doc_id: str) -> Path:

        return self.documents_dir / f"{doc_id}.json"

    def _get_chroma_client(self):
        """获取 ChromaDB 客户端"""
        if self._chroma_client is None and CHROMA_AVAILABLE:
            chroma_path = self.base_dir / "chroma"
            chroma_path.mkdir(parents=True, exist_ok=True)
            self._chroma_client = chromadb.PersistentClient(
                path=str(chroma_path),
                settings=Settings(anonymized_telemetry=False)
            )
        return self._chroma_client

    def _get_collection(self, base_id: str):
        """获取或创建指定知识库的 collection"""
        client = self._get_chroma_client()
        if client is None:
            return None
        try:
            return client.get_or_create_collection(
                name=f"kb_{base_id}",
                metadata={"hnsw:space": "cosine"}
            )
        except Exception as e:
            _log.error(f"[Chroma] Failed to get collection: {e}")
            return None

    def set_embedding_service(self, service: EmbeddingService):
        """设置 embedding 服务"""
        self._embedding_service = service

    def save_base(self, base: KnowledgeBase):
        """保存知识库元数据"""
        file_path = self._get_base_file(base.id)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump({
                "id": base.id,
                "name": base.name,
                "description": base.description,
                "owner_type": base.owner_type,
                "owner_id": base.owner_id,
                "documents": base.documents,
                "created_at": base.created_at,
                "updated_at": base.updated_at,
                "metadata": base.metadata
            }, f, ensure_ascii=False, indent=2)

    def load_base(self, base_id: str) -> Optional[KnowledgeBase]:
        """加载知识库元数据"""
        file_path = self._get_base_file(base_id)
        if not file_path.exists():
            return None

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return KnowledgeBase(**data)

    def delete_base(self, base_id: str) -> bool:
        """删除知识库"""
        # 删除元数据文件
        file_path = self._get_base_file(base_id)
        if file_path.exists():
            file_path.unlink()

        # 删除 ChromaDB collection
        client = self._get_chroma_client()
        if client:
            try:
                client.delete_collection(f"kb_{base_id}")
            except Exception:
                pass

        return True

    def save_document(self, doc: Document):
        """保存文档元数据"""
        file_path = self._get_doc_file(doc.id)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(doc.__dict__, f, ensure_ascii=False, indent=2)

    def load_document(self, doc_id: str) -> Optional[Document]:
        """加载文档元数据"""
        file_path = self._get_doc_file(doc_id)
        if not file_path.exists():
            return None

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return Document(**data)

    def add_chunks_to_chroma(self, base_id: str, doc_id: str, chunks: List[str]):
        """将文档分块添加到 ChromaDB"""
        if not CHROMA_AVAILABLE:
            return

        collection = self._get_collection(base_id)
        if collection is None:
            return

        # 获取 embeddings
        if self._embedding_service:
            embeddings = self._embedding_service.get_embeddings(chunks)
        else:
            # 使用本地简单向量化，避免下载 ONNX 模型
            embeddings = [self._local_embedding(text) for text in chunks]

        # 添加到 collection
        ids = [f"{doc_id}_{i}" for i in range(len(chunks))]
        metadatas = [{"doc_id": doc_id, "chunk_index": i} for i in range(len(chunks))]

        try:
            collection.add(
                ids=ids,
                documents=chunks,
                embeddings=embeddings,
                metadatas=metadatas
            )
        except Exception as e:
            _log.error(f"[Chroma] Failed to add chunks: {e}")

    def _local_embedding(self, text: str) -> List[float]:
        """本地简单向量化（不依赖外部模型）"""
        words = re.findall(r'[\w]+', text.lower())
        vector = [0.0] * 256
        for word in set(words):
            hash_val = int(hashlib.md5(word.encode()).hexdigest()[:8], 16)
            idx = hash_val % 256
            vector[idx] += 1.0
        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]
        return vector

    def search_chunks(self, base_id: str, query: str, top_k: int = 5) -> List[Tuple[str, float, Dict]]:
        """搜索相关分块"""
        if not CHROMA_AVAILABLE:
            return []

        collection = self._get_collection(base_id)
        if collection is None:
            return []

        try:
            # 获取查询的 embedding
            if self._embedding_service:
                query_embedding = self._embedding_service.get_embeddings([query])[0]
            else:
                query_embedding = self._local_embedding(query)
            results = collection.query(
                query_embeddings=[query_embedding],
                n_results=top_k,
                include=["documents", "distances", "metadatas"]
            )

            # 格式化结果
            formatted = []
            if results and results.get("documents"):
                for i, doc in enumerate(results["documents"][0]):
                    distance = results["distances"][0][i] if results.get("distances") else 0
                    metadata = results["metadatas"][0][i] if results.get("metadatas") else {}
                    # 转换距离为相似度 (cosine distance -> similarity)
                    similarity = 1.0 - distance
                    formatted.append((doc, similarity, metadata))

            return formatted
        except Exception as e:
            err_msg = str(e)
            # 维度不匹配是 embedding 模型切换导致的已知问题，静默处理
            if "dimension" in err_msg.lower() or "embedding" in err_msg.lower():
                _log.debug(f"[Chroma] Search skipped (dimension mismatch): {e}")
            else:
                _log.error(f"[Chroma] Search failed: {e}")
            return []

    def delete_document_chunks(self, base_id: str, doc_id: str):
        """删除文档的所有分块"""
        if not CHROMA_AVAILABLE:
            return

        collection = self._get_collection(base_id)
        if collection is None:
            return

        try:
            # 查询该文档的所有 chunk id
            results = collection.get(
                where={"doc_id": doc_id}
            )
            if results and results.get("ids"):
                collection.delete(ids=results["ids"])
        except Exception as e:
            _log.warning(f"[Chroma] Failed to delete document chunks: {e}")


class KnowledgeManager:
    """知识库管理器"""

    def __init__(self):
        self.store = ChromaKnowledgeStore()
        self._init_default_knowledge()

    def _init_default_knowledge(self):
        """初始化默认知识"""
        default_kb_id = "default"
        if not self.store.load_base(default_kb_id):
            kb = KnowledgeBase(
                id=default_kb_id,
                name="默认知识库",
                description="系统默认知识库",
                owner_type="system"
            )
            self.store.save_base(kb)

    def _generate_id(self, text: str) -> str:
        """生成ID"""
        return hashlib.md5(text.encode()).hexdigest()[:16]

    def configure_embedding(
        self,
        api_key: str = None,
        base_url: str = None,
        model: str = None,
        proxy_url: str = None,
    ):
        """配置 embedding 服务
        
        如果没有提供参数，会自动从配置中加载
        """
        # 尝试从新的配置架构加载
        if NEW_CONFIG_AVAILABLE:
            try:
                embedding_config = get_embedding_model_config()
                if embedding_config:
                    api_key = api_key or embedding_config.get("api_key", "")
                    base_url = base_url or embedding_config.get("base_url", "")
                    model = model or embedding_config.get("model", "text-embedding-3-small")
                    proxy_url = proxy_url or embedding_config.get("proxy_url", "")
                    _log.info(f"[Knowledge] Loaded embedding config from new architecture: model={model}")
            except Exception as e:
                _log.warning(f"[Knowledge] Failed to load embedding config from new architecture: {e}")
        
        if model:
            service = EmbeddingService(
                api_key=api_key,
                base_url=base_url,
                model=model,
                proxy_url=proxy_url,
            )
            self.store.set_embedding_service(service)
            _log.info(f"[Knowledge] Embedding service configured: model={model}")
        else:
            self.store.set_embedding_service(None)
            _log.info("[Knowledge] Embedding service disabled")

    def create_knowledge_base(
        self,
        name: str,
        description: str = "",
        user_id: str = None,
        group_id: str = None
    ) -> KnowledgeBase:
        """创建知识库"""
        owner_type = "user" if user_id else "group"
        owner_id = user_id or group_id

        kb_id = self._generate_id(f"{name}_{owner_type}_{owner_id}")

        kb = KnowledgeBase(
            id=kb_id,
            name=name,
            description=description,
            owner_type=owner_type,
            owner_id=owner_id
        )

        self.store.save_base(kb)
        _log.info(f"[Knowledge] Created knowledge base: {kb_id}")

        return kb

    def add_document(
        self,
        base_id: str,
        title: str,
        content: str,
        source: str = "",
        tags: List[str] = None
    ) -> Document:
        """添加文档到知识库"""
        kb = self.store.load_base(base_id)
        if not kb:
            raise ValueError(f"Knowledge base not found: {base_id}")

        doc_id = self._generate_id(f"{title}_{content[:100]}_{datetime.now().isoformat()}")

        doc = Document(
            id=doc_id,
            title=title,
            content=content,
            source=source,
            tags=tags or []
        )

        self.store.save_document(doc)

        # 分块并添加到 ChromaDB
        chunks = TextProcessor.chunk_text(content)
        self.store.add_chunks_to_chroma(base_id, doc_id, chunks)

        # 更新知识库元数据
        kb.documents.append(doc_id)
        kb.updated_at = datetime.now().isoformat()
        self.store.save_base(kb)

        _log.info(f"[Knowledge] Added document to base {base_id}: {doc_id} ({len(chunks)} chunks)")

        return doc

    def delete_document(self, base_id: str, doc_id: str) -> bool:
        """从知识库删除文档"""
        kb = self.store.load_base(base_id)
        if not kb:
            return False

        # 从元数据中移除
        if doc_id in kb.documents:
            kb.documents.remove(doc_id)
            kb.updated_at = datetime.now().isoformat()
            self.store.save_base(kb)

        # 删除 ChromaDB 中的分块
        self.store.delete_document_chunks(base_id, doc_id)

        # 删除文档文件
        doc_file = self.store._get_doc_file(doc_id)
        if doc_file.exists():
            doc_file.unlink()

        return True

    def search(
        self,
        query: str,
        base_id: str = None,
        top_k: int = 3,
        **options: Any,
    ) -> List[Tuple[Document, float, str]]:
        """检索知识库（兼容旧接口：``[(文档, 分数, 片段), ...]``）。

        内部走 :meth:`retrieve`，会自动使用已配置的 rerank 模型并做 MMR 去重；
        传 ``rerank=False`` / ``mmr=False`` 可关闭，``candidates=N`` 控制候选池大小。
        """
        payload = self.retrieve(query, base_id=base_id, top_k=top_k, **options)
        return payload["results"]

    def search_with_citations(
        self,
        query: str,
        base_id: str = None,
        top_k: int = 3,
        **options: Any,
    ) -> Dict[str, Any]:
        """检索并返回引用信息（doc id / 标题 / 片段位置 / 分数），见 :meth:`retrieve`。"""
        return self.retrieve(query, base_id=base_id, top_k=top_k, **options)

    def retrieve(
        self,
        query: str,
        base_id: str = None,
        top_k: int = 3,
        *,
        rerank: Optional[bool] = None,
        mmr: Optional[bool] = None,
        candidates: Optional[int] = None,
        max_per_doc: int = 1,
        lambda_param: float = DEFAULT_MMR_LAMBDA,
    ) -> Dict[str, Any]:
        """检索知识库，返回带 citations 的结构化结果。

        Args:
            query: 查询文本
            base_id: 知识库 ID；为空时只用 ``default`` 库（与旧行为一致）
            top_k: 返回条数
            rerank: ``None`` 自动（配置了 rerank 模型就重排）/ ``True`` 强制 / ``False`` 关闭
            mmr: ``None`` 自动（候选多于 top_k 时去重）/ ``False`` 关闭
            candidates: 候选池大小，默认按是否启用增强自动取 ``top_k*2`` 或 ``top_k*4``
            max_per_doc: 同一文档最多保留几个片段
            lambda_param: MMR 相关度权重（0~1，越大多样性越强）

        Returns:
            ``{"query", "results": [(文档, 分数, 片段)], "citations": [...], "meta": {...}}``
        """
        top_k = max(1, int(top_k or 1))
        max_per_doc = max(1, int(max_per_doc or 1))
        try:
            lambda_value = min(max(float(lambda_param), 0.0), 1.0)
        except (TypeError, ValueError):
            lambda_value = DEFAULT_MMR_LAMBDA

        # 确定要搜索的知识库
        if base_id:
            kb = self.store.load_base(base_id)
            bases = [kb] if kb else []
        else:
            kb = self.store.load_base("default")
            bases = [kb] if kb else []

        service = get_rerank_service() if rerank is not False else None
        use_mmr = bool(mmr) if mmr is not None else True
        if candidates:
            pool_size = max(top_k, int(candidates))
        elif service is not None or use_mmr:
            pool_size = max(top_k * 4, top_k)
        else:
            pool_size = top_k * 2

        doc_cache: Dict[str, Optional[Document]] = {}
        pool: List[Dict[str, Any]] = []

        for kb in bases:
            if not kb:
                continue

            # 从 ChromaDB 搜索
            chunk_results = self.store.search_chunks(kb.id, query, top_k=pool_size)

            for chunk_content, similarity, metadata in chunk_results:
                metadata = metadata or {}
                doc_id = metadata.get("doc_id", "")
                if not doc_id:
                    continue
                if doc_id not in doc_cache:
                    doc_cache[doc_id] = self.store.load_document(doc_id)
                doc = doc_cache[doc_id]
                if doc is None:
                    continue
                try:
                    chunk_index = int(metadata.get("chunk_index", -1))
                except (TypeError, ValueError):
                    chunk_index = -1
                score = float(similarity or 0.0)
                pool.append({
                    "doc": doc,
                    "base_id": kb.id,
                    "chunk": chunk_content,
                    "chunk_index": chunk_index,
                    "similarity": score,
                    "score": score,
                    "rerank_score": None,
                })

        pool.sort(key=lambda item: item["score"], reverse=True)

        # rerank：模型重排（未配置或失败时保持向量相似度顺序）
        rerank_meta: Dict[str, Any] = {
            "enabled": service is not None,
            "model": getattr(service, "model", "") if service is not None else "",
            "applied": False,
            "error": "",
        }
        if service is not None and pool:
            texts = [item["chunk"] for item in pool]
            scores = service.scores_for(query, texts)
            if scores:
                values = list(scores.values())
                highest, lowest = max(values), min(values)
                span = (highest - lowest) or 1.0
                weight = min(max(float(getattr(service, "weight", 1.0)), 0.0), 1.0)
                for index, item in enumerate(pool):
                    raw = scores.get(index)
                    item["rerank_score"] = raw
                    if raw is None:
                        item["score"] = (1.0 - weight) * item["similarity"]
                        continue
                    normalized = (raw - lowest) / span if highest > lowest else 1.0
                    item["score"] = (
                        weight * normalized + (1.0 - weight) * item["similarity"]
                    )
                pool.sort(key=lambda item: item["score"], reverse=True)
                rerank_meta["applied"] = True
            else:
                rerank_meta["error"] = "rerank 请求失败或没有可用结果"

        # MMR：同文档/近重复片段先去重，再按相关度-冗余度平衡挑选
        mmr_meta = {"enabled": use_mmr, "lambda": round(lambda_value, 3), "applied": False}
        chosen: List[Dict[str, Any]] = pool
        for item in pool:
            item["text"] = item["chunk"]
        if use_mmr and len(pool) > top_k:
            picked_count = min(len(pool), max(top_k * 2, top_k + 2))
            chosen = mmr_select(
                pool,
                picked_count,
                lambda_param=lambda_value,
            )
            mmr_meta["applied"] = True
        elif len(pool) > 1:
            chosen = drop_near_duplicates(pool)

        # 同一文档最多 max_per_doc 个片段（旧行为默认每篇只留最相关的一段）
        results: List[Tuple[Document, float, str]] = []
        citations: List[Dict[str, Any]] = []
        per_doc: Dict[str, int] = {}
        for item in chosen:
            doc_id = item["doc"].id
            if per_doc.get(doc_id, 0) >= max_per_doc:
                continue
            per_doc[doc_id] = per_doc.get(doc_id, 0) + 1
            results.append((item["doc"], item["score"], item["chunk"]))
            citations.append(self._build_citation(item, len(citations) + 1))
            if len(results) >= top_k:
                break

        return {
            "query": query,
            "results": results,
            "citations": citations,
            "meta": {
                "top_k": top_k,
                "candidates": len(pool),
                "max_per_doc": max_per_doc,
                "bases": [kb.id for kb in bases if kb],
                "rerank": rerank_meta,
                "mmr": mmr_meta,
            },
        }

    @staticmethod
    def _build_citation(item: Dict[str, Any], position: int) -> Dict[str, Any]:
        """组装一条引用：文档 id / 标题 / 片段位置（分块序号 + 字符区间）/ 分数。"""
        doc = item["doc"]
        chunk = str(item.get("chunk") or "")
        chunk_index = int(item.get("chunk_index", -1))
        start, end = KnowledgeManager._chunk_offsets(doc.content or "", chunk, chunk_index)
        citation = {
            "id": f"K{position}",
            "doc_id": doc.id,
            "title": doc.title,
            "source": getattr(doc, "source", "") or "",
            "base_id": item.get("base_id", ""),
            "chunk_index": chunk_index,
            "start_char": start,
            "end_char": end,
            "chars": len(chunk),
            "snippet": chunk[:200] + ("..." if len(chunk) > 200 else ""),
            "score": round(float(item.get("score", 0.0)), 4),
            "similarity": round(float(item.get("similarity", 0.0)), 4),
            "rerank_score": (
                round(float(item["rerank_score"]), 4)
                if item.get("rerank_score") is not None
                else None
            ),
            "tags": list(getattr(doc, "tags", None) or []),
        }
        return citation

    @staticmethod
    def _chunk_offsets(content: str, chunk: str, chunk_index: int) -> Tuple[int, int]:
        """估算片段在原文中的字符区间；找不到时按分块参数回退，未知返回 (-1, -1)。"""
        if not chunk or not content:
            return -1, -1
        start = content.find(chunk)
        if start < 0:
            # 分块时 strip 过，用去掉首尾空白的前缀再试一次
            probe = chunk.strip()[:60]
            start = content.find(probe) if probe else -1
        if start < 0 and chunk_index >= 0:
            # TextProcessor.chunk_text 默认 chunk_size=500 / overlap=50
            start = min(chunk_index * 450, max(0, len(content) - len(chunk)))
        if start < 0:
            return -1, -1
        return start, min(len(content), start + len(chunk))

    def list_knowledge_bases(
        self,
        user_id: str = None,
        group_id: str = None
    ) -> List[KnowledgeBase]:
        """列出知识库"""
        owner_id = user_id or group_id
        owner_type = "user" if user_id else "group"

        bases = []
        for file in self.store.bases_dir.glob("*.json"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    kb = KnowledgeBase(**data)
                    if kb.owner_id == owner_id or kb.owner_type == "system":
                        bases.append(kb)
            except Exception:
                continue

        return bases

    def delete_knowledge_base(self, base_id: str) -> bool:
        """删除知识库"""
        kb = self.store.load_base(base_id)
        if not kb:
            return False

        # 删除所有文档
        for doc_id in kb.documents:
            doc_file = self.store._get_doc_file(doc_id)
            if doc_file.exists():
                doc_file.unlink()

        # 删除知识库（包括 ChromaDB collection）
        return self.store.delete_base(base_id)

    def get_stats(self, base_id: str = None) -> Dict[str, Any]:
        """获取知识库统计信息"""
        if base_id:
            kb = self.store.load_base(base_id)
            bases = [kb] if kb else []
        else:
            bases = []
            for file in self.store.bases_dir.glob("*.json"):
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        bases.append(KnowledgeBase(**data))
                except Exception:
                    continue

        total_docs = 0
        total_bases = len(bases)

        for kb in bases:
            total_docs += len(kb.documents)

        return {
            "total_bases": total_bases,
            "total_documents": total_docs,
            "chroma_available": CHROMA_AVAILABLE
        }

    def rebuild_index(self, base_id: str = None) -> Dict[str, Any]:
        """
        重建知识库的向量索引
        用于迁移旧数据或修复损坏的索引
        """
        if not CHROMA_AVAILABLE:
            return {"success": False, "error": "ChromaDB not available"}

        if base_id:
            kb = self.store.load_base(base_id)
            bases = [kb] if kb else []
        else:
            # 重建所有知识库
            bases = []
            for file in self.store.bases_dir.glob("*.json"):
                try:
                    with open(file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        bases.append(KnowledgeBase(**data))
                except Exception:
                    continue

        rebuilt_docs = 0
        rebuilt_chunks = 0

        for kb in bases:
            if not kb:
                continue

            _log.info(f"[Knowledge] Rebuilding index for: {kb.name}")

            for doc_id in kb.documents:
                doc = self.store.load_document(doc_id)
                if not doc:
                    continue

                # 先删除旧的索引
                self.store.delete_document_chunks(kb.id, doc_id)

                # 重新分块并索引
                chunks = TextProcessor.chunk_text(doc.content)
                self.store.add_chunks_to_chroma(kb.id, doc_id, chunks)

                rebuilt_docs += 1
                rebuilt_chunks += len(chunks)

        _log.info(f"[Knowledge] Rebuild complete: {rebuilt_docs} docs, {rebuilt_chunks} chunks")

        return {
            "success": True,
            "rebuilt_documents": rebuilt_docs,
            "rebuilt_chunks": rebuilt_chunks
        }

    def check_and_rebuild_if_needed(self) -> bool:
        """
        检查是否需要重建索引，如果需要则自动重建
        返回是否执行了重建
        """
        if not CHROMA_AVAILABLE:
            return False

        try:
            # 获取默认知识库
            kb = self.store.load_base("default")
            if not kb or not kb.documents:
                return False

            # 检查 ChromaDB 中是否有数据
            collection = self.store._get_collection("default")
            if collection is None:
                return False

            existing_count = collection.count()

            # 如果文档数不一致，需要重建
            if existing_count == 0 and len(kb.documents) > 0:
                _log.info(f"[Knowledge] Detected {len(kb.documents)} docs without vector index, rebuilding...")
                self.rebuild_index("default")
                return True

            return False
        except Exception as e:
            _log.warning(f"[Knowledge] Check rebuild failed: {e}")
            return False


# 全局单例
knowledge_manager: Optional[KnowledgeManager] = None


def get_knowledge_manager() -> KnowledgeManager:
    """获取知识库管理器单例"""
    global knowledge_manager
    if knowledge_manager is None:
        knowledge_manager = KnowledgeManager()
    return knowledge_manager


def configure_knowledge_embedding(
    api_key: str, base_url: str, model: str, proxy_url: str = ""
):
    """配置知识库的 embedding 服务（快捷方法）"""
    km = get_knowledge_manager()
    km.configure_embedding(
        api_key=api_key,
        base_url=base_url,
        model=model,
        proxy_url=proxy_url,
    )
