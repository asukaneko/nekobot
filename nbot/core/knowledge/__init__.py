"""
知识库系统 (RAG - Retrieval Augmented Generation)
支持用户创建和管理知识库，进行向量检索
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

_log = logging.getLogger(__name__)


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
class Chunk:
    """文档分块"""
    id: str
    document_id: str
    content: str
    index: int
    vector: List[float] = field(default_factory=list)
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
                newline_pos = text.rfind('\n', start, end)
                if newline_pos > start:
                    end = newline_pos

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


class SimpleVectorizer:
    """简单的向量化器 - 基于词频"""

    def __init__(self):
        self.vocab: Dict[str, int] = {}
        self.doc_vectors: Dict[str, List[float]] = {}

    def _tokenize(self, text: str) -> List[str]:
        """简单分词"""
        text = text.lower()
        words = re.findall(r'[\w]+', text)
        return words

    def _build_vocab(self, documents: List[str]):
        """构建词表"""
        vocab = {}
        for doc in documents:
            words = self._tokenize(doc)
            for word in set(words):
                if word not in vocab:
                    vocab[word] = len(vocab)
        self.vocab = vocab

    def _text_to_vector(self, text: str) -> List[float]:
        """文本转向量"""
        words = self._tokenize(text)
        vector = [0.0] * len(self.vocab)

        for word in words:
            if word in self.vocab:
                vector[self.vocab[word]] += 1

        norm = sum(v * v for v in vector) ** 0.5
        if norm > 0:
            vector = [v / norm for v in vector]

        return vector

    def fit_transform(self, documents: List[str], doc_ids: List[str]) -> Dict[str, List[float]]:
        """拟合并转换"""
        self._build_vocab(documents)

        vectors = {}
        for doc_id, doc in zip(doc_ids, documents):
            vectors[doc_id] = self._text_to_vector(doc)

        self.doc_vectors = vectors
        return vectors

    def transform(self, text: str) -> List[float]:
        """转换单个文本"""
        return self._text_to_vector(text)

    @staticmethod
    def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
        """计算余弦相似度"""
        if not vec1 or not vec2:
            return 0.0

        dot = sum(a * b for a, b in zip(vec1, vec2))
        norm1 = sum(a * a for a in vec1) ** 0.5
        norm2 = sum(b * b for b in vec2) ** 0.5

        if norm1 == 0 or norm2 == 0:
            return 0.0

        return dot / (norm1 * norm2)


class KnowledgeBaseStore:
    """知识库存储"""

    def __init__(self, base_dir: str = "saved_message/knowledge"):
        self.base_dir = Path(base_dir)
        self.bases_dir = self.base_dir / "bases"
        self.documents_dir = self.base_dir / "documents"
        self.chunks_dir = self.base_dir / "chunks"

        self.bases_dir.mkdir(parents=True, exist_ok=True)
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.chunks_dir.mkdir(parents=True, exist_ok=True)

    def _get_base_file(self, base_id: str) -> Path:
        return self.bases_dir / f"{base_id}.json"

    def _get_doc_file(self, doc_id: str) -> Path:
        return self.documents_dir / f"{doc_id}.json"

    def _get_chunk_file(self, chunk_id: str) -> Path:
        return self.chunks_dir / f"{chunk_id}.json"

    def save_base(self, base: KnowledgeBase):
        """保存知识库"""
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
        """加载知识库"""
        file_path = self._get_base_file(base_id)
        if not file_path.exists():
            return None

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return KnowledgeBase(**data)

    def delete_base(self, base_id: str) -> bool:
        """删除知识库"""
        file_path = self._get_base_file(base_id)
        if file_path.exists():
            file_path.unlink()
            return True
        return False

    def save_document(self, doc: Document):
        """保存文档"""
        file_path = self._get_doc_file(doc.id)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(doc.__dict__, f, ensure_ascii=False, indent=2)

    def load_document(self, doc_id: str) -> Optional[Document]:
        """加载文档"""
        file_path = self._get_doc_file(doc_id)
        if not file_path.exists():
            return None

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return Document(**data)

    def save_chunk(self, chunk: Chunk):
        """保存分块"""
        file_path = self._get_chunk_file(chunk.id)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(chunk.__dict__, f, ensure_ascii=False, indent=2)

    def load_chunk(self, chunk_id: str) -> Optional[Chunk]:
        """加载分块"""
        file_path = self._get_chunk_file(chunk_id)
        if not file_path.exists():
            return None

        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return Chunk(**data)


class KnowledgeManager:
    """知识库管理器"""

    def __init__(self):
        self.store = KnowledgeBaseStore()
        self.vectorizer = SimpleVectorizer()
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
        _log.info(f"Created knowledge base: {kb_id}")

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

        doc_id = self._generate_id(content)

        doc = Document(
            id=doc_id,
            title=title,
            content=content,
            source=source,
            tags=tags or []
        )

        self.store.save_document(doc)

        chunks = TextProcessor.chunk_text(content)
        for i, chunk_content in enumerate(chunks):
            chunk_id = f"{doc_id}_{i}"
            chunk = Chunk(
                id=chunk_id,
                document_id=doc_id,
                content=chunk_content,
                index=i
            )
            self.store.save_chunk(chunk)

        kb.documents.append(doc_id)
        kb.updated_at = datetime.now().isoformat()
        self.store.save_base(kb)

        _log.info(f"Added document to base {base_id}: {doc_id}")

        return doc

    def search(
        self,
        query: str,
        base_id: str = None,
        top_k: int = 3
    ) -> List[Tuple[Document, float, str]]:
        """检索知识库"""
        if base_id:
            bases = [self.store.load_base(base_id)] if self.store.load_base(base_id) else []
        else:
            bases = [self.store.load_base("default")]

        all_chunks = []
        for kb in bases:
            if not kb:
                continue
            for doc_id in kb.documents:
                doc = self.store.load_document(doc_id)
                if not doc:
                    continue

                chunks = []
                for i in range(len(TextProcessor.chunk_text(doc.content))):
                    chunk_id = f"{doc_id}_{i}"
                    chunk = self.store.load_chunk(chunk_id)
                    if chunk:
                        chunks.append((chunk, doc))

                all_chunks.extend(chunks)

        if not all_chunks:
            return []

        chunk_texts = [c[0].content for c in all_chunks]
        chunk_ids = [c[0].id for c in all_chunks]

        self.vectorizer.fit_transform(chunk_texts, chunk_ids)

        query_vector = self.vectorizer.transform(query)

        results = []
        for chunk, doc in all_chunks:
            if not chunk.vector:
                chunk.vector = self.vectorizer.transform(chunk.content)

            sim = SimpleVectorizer.cosine_similarity(query_vector, chunk.vector)
            results.append((doc, sim, chunk.content))

        results.sort(key=lambda x: x[1], reverse=True)

        unique_results = []
        seen_docs = set()
        for doc, sim, chunk in results:
            if doc.id not in seen_docs:
                seen_docs.add(doc.id)
                unique_results.append((doc, sim, chunk))

        return unique_results[:top_k]

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
            with open(file, "r", encoding="utf-8") as f:
                data = json.load(f)
                kb = KnowledgeBase(**data)
                if kb.owner_id == owner_id or kb.owner_type == "system":
                    bases.append(kb)

        return bases

    def delete_knowledge_base(self, base_id: str) -> bool:
        """删除知识库"""
        return self.store.delete_base(base_id)


knowledge_manager: Optional[KnowledgeManager] = None


def get_knowledge_manager() -> KnowledgeManager:
    """获取知识库管理器单例"""
    global knowledge_manager
    if knowledge_manager is None:
        knowledge_manager = KnowledgeManager()
    return knowledge_manager
