import logging
from datetime import datetime

from flask import jsonify, request
from nbot.core.knowledge import TextProcessor

_log = logging.getLogger(__name__)


def register_knowledge_routes(app, server):
    @app.route("/api/knowledge")
    def get_knowledge():
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify([])
        try:
            km = server.get_knowledge_manager()
            bases = km.list_knowledge_bases()
            docs = []
            for kb in bases:
                for doc_id in kb.documents:
                    doc = km.store.load_document(doc_id)
                    if doc:
                        docs.append(
                            {
                                "id": doc.id,
                                "name": doc.title,
                                "title": doc.title,
                                "type": doc.source or "txt",
                                "source": doc.source,
                                "size": len(doc.content),
                                "content": doc.content[:200] + "..."
                                if len(doc.content) > 200
                                else doc.content,
                                "full_content": doc.content,
                                "description": "",
                                "indexed": True,
                                "tags": doc.tags,
                                "created_at": doc.created_at,
                            }
                        )
            return jsonify(docs)
        except Exception as e:
            _log.error(f"Failed to get knowledge: {e}")
            return jsonify([])

    @app.route("/api/knowledge", methods=["POST"])
    def add_knowledge():
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            data = request.json or {}
            km = server.get_knowledge_manager()
            title = data.get("name", "新文档")
            content = data.get("content", "")
            source = data.get("source", "")
            tags = data.get("tags", [])

            if not content:
                return jsonify({"success": False, "error": "Content is required"}), 400

            default_kb = km.store.load_base("default")
            if not default_kb:
                km.create_knowledge_base("默认知识库", "系统默认知识库")

            doc = km.add_document("default", title, content, source, tags)

            return jsonify(
                {
                    "success": True,
                    "document": {
                        "id": doc.id,
                        "name": doc.title,
                        "title": doc.title,
                        "type": doc.source or "txt",
                        "source": doc.source,
                        "size": len(doc.content),
                        "content": doc.content,
                        "description": "",
                        "indexed": True,
                        "tags": doc.tags,
                        "created_at": doc.created_at,
                    },
                }
            )
        except Exception as e:
            _log.error(f"Failed to add knowledge: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/knowledge/<doc_id>")
    def get_knowledge_doc(doc_id):
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify({"error": "Knowledge manager not available"}), 503
        try:
            km = server.get_knowledge_manager()
            doc = km.store.load_document(doc_id)
            if doc:
                return jsonify(
                    {
                        "id": doc.id,
                        "name": doc.title,
                        "title": doc.title,
                        "type": doc.source or "txt",
                        "source": doc.source,
                        "size": len(doc.content),
                        "content": doc.content,
                        "description": "",
                        "indexed": True,
                        "tags": doc.tags,
                        "created_at": doc.created_at,
                    }
                )
            return jsonify({"error": "Document not found"}), 404
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/knowledge/<doc_id>", methods=["PUT"])
    def update_knowledge_doc(doc_id):
        """更新文档内容"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify({"error": "Knowledge manager not available"}), 503
        try:
            data = request.json or {}
            km = server.get_knowledge_manager()
            doc = km.store.load_document(doc_id)
            if not doc:
                return jsonify({"error": "Document not found"}), 404

            # 更新文档字段
            if "title" in data or "name" in data:
                doc.title = data.get("title") or data.get("name", doc.title)
            if "content" in data:
                doc.content = data["content"]
            if "source" in data:
                doc.source = data["source"]
            if "tags" in data:
                doc.tags = data["tags"]

            km.store.save_document(doc)

            # 如果内容变了，重新分块索引
            if "content" in data:
                km.store.delete_document_chunks("default", doc_id)
                chunks = TextProcessor.chunk_text(doc.content)
                km.store.add_chunks_to_chroma("default", doc_id, chunks)

            return jsonify({
                "success": True,
                "document": {
                    "id": doc.id,
                    "name": doc.title,
                    "title": doc.title,
                    "type": doc.source or "txt",
                    "source": doc.source,
                    "size": len(doc.content),
                    "content": doc.content,
                    "description": "",
                    "indexed": True,
                    "tags": doc.tags,
                    "created_at": doc.created_at,
                }
            })
        except Exception as e:
            _log.error(f"[Knowledge] Update doc failed: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/knowledge/<doc_id>", methods=["DELETE"])
    def delete_knowledge(doc_id):
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            km = server.get_knowledge_manager()
            success = km.delete_document("default", doc_id)
            if success:
                return jsonify({"success": True})
            return jsonify({"success": False, "error": "Document not found"}), 404
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/knowledge/<doc_id>/index", methods=["POST"])
    def index_knowledge(doc_id):
        """手动为单篇文档重建向量索引"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            km = server.get_knowledge_manager()
            base_id = request.args.get("base_id", "default")
            doc = km.store.load_document(doc_id)
            if not doc:
                return jsonify({"success": False, "error": "Document not found"}), 404

            # 删除旧索引
            km.store.delete_document_chunks(base_id, doc_id)
            # 重新分块并索引
            chunks = TextProcessor.chunk_text(doc.content)
            km.store.add_chunks_to_chroma(base_id, doc_id, chunks)
            _log.info(f"[Knowledge] Indexed doc {doc_id}: {len(chunks)} chunks")
            return jsonify({
                "success": True,
                "doc_id": doc_id,
                "chunks": len(chunks)
            })
        except Exception as e:
            _log.error(f"[Knowledge] Index doc failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/knowledge/batch", methods=["POST"])
    def batch_add_knowledge():
        """批量导入文档"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            data = request.json or {}
            base_id = data.get("base_id", "default")
            km = server.get_knowledge_manager()

            # 确保持续库存在
            kb = km.store.load_base(base_id)
            if not kb:
                km.create_knowledge_base(
                    name=base_id if base_id != "default" else "默认知识库",
                    description="系统默认知识库"
                )
                kb = km.store.load_base(base_id)

            documents = data.get("documents", [])
            if not documents:
                return jsonify({"success": False, "error": "No documents provided"}), 400

            # 支持两种格式：
            # 1. { "documents": [{ "title": "...", "content": "...", "source": "...", "tags": [...] }, ...] }
            # 2. { "items": [{ "name": "...", "content": "..." }, ...] }  (前端兼容格式)
            results = []
            errors = []

            for item in documents:
                # 兼容前端 "name" 字段
                title = item.get("title") or item.get("name", "未命名文档")
                content = item.get("content", "")
                source = item.get("source", "")
                tags = item.get("tags", [])

                if not content:
                    errors.append({"title": title, "error": "Empty content"})
                    continue

                try:
                    doc = km.add_document(base_id, title, content, source, tags)
                    results.append({
                        "id": doc.id,
                        "title": doc.title,
                        "chunks": len(TextProcessor.chunk_text(content))
                    })
                except Exception as e:
                    errors.append({"title": title, "error": str(e)})

            return jsonify({
                "success": True,
                "imported": len(results),
                "failed": len(errors),
                "documents": results,
                "errors": errors
            })
        except Exception as e:
            _log.error(f"[Knowledge] Batch import failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/knowledge/batch-delete", methods=["POST"])
    def batch_delete_knowledge():
        """批量删除文档"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            data = request.json or {}
            base_id = data.get("base_id", "default")
            doc_ids = data.get("doc_ids", [])

            if not doc_ids:
                return jsonify({"success": False, "error": "No doc_ids provided"}), 400

            km = server.get_knowledge_manager()
            deleted = []
            failed = []

            for doc_id in doc_ids:
                try:
                    if km.delete_document(base_id, doc_id):
                        deleted.append(doc_id)
                    else:
                        failed.append({"doc_id": doc_id, "error": "Not found"})
                except Exception as e:
                    failed.append({"doc_id": doc_id, "error": str(e)})

            return jsonify({
                "success": True,
                "deleted": len(deleted),
                "failed": len(failed),
                "doc_ids": deleted,
                "errors": failed
            })
        except Exception as e:
            _log.error(f"[Knowledge] Batch delete failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/knowledge/stats")
    def knowledge_stats():
        """获取知识库统计信息"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify({"error": "Knowledge manager not available"}), 503
        try:
            km = server.get_knowledge_manager()
            stats = km.get_stats()
            return jsonify(stats)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/knowledge/search", methods=["POST"])
    def search_knowledge():
        """搜索知识库"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            data = request.json or {}
            query = data.get("query", "")
            top_k = data.get("top_k", 5)

            if not query:
                return jsonify({"success": False, "error": "Query is required"}), 400

            km = server.get_knowledge_manager()
            results = km.search(query, top_k=top_k)

            return jsonify({
                "success": True,
                "results": [
                    {
                        "doc_id": doc.id,
                        "title": doc.title,
                        "similarity": round(sim, 4),
                        "chunk": chunk[:200] + "..." if len(chunk) > 200 else chunk,
                    }
                    for doc, sim, chunk in results
                ]
            })
        except Exception as e:
            _log.error(f"Search failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/knowledge/rebuild", methods=["POST"])
    def rebuild_knowledge_index():
        """重建知识库向量索引"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            km = server.get_knowledge_manager()
            result = km.rebuild_index()
            return jsonify(result)
        except Exception as e:
            _log.error(f"Rebuild failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

    @app.route("/api/knowledge/export")
    def export_knowledge():
        """导出所有知识库文档为 JSON（可重新导入）"""
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        try:
            km = server.get_knowledge_manager()
            bases = km.list_knowledge_bases()
            all_docs = []
            for kb in bases:
                for doc_id in kb.documents:
                    doc = km.store.load_document(doc_id)
                    if doc:
                        all_docs.append({
                            "name": doc.title,
                            "title": doc.title,
                            "content": doc.content,
                            "source": doc.source or "",
                            "tags": doc.tags or [],
                            "created_at": doc.created_at
                        })
            return jsonify({
                "success": True,
                "version": "1.0",
                "exported_at": datetime.now().isoformat(),
                "total": len(all_docs),
                "documents": all_docs
            })
        except Exception as e:
            _log.error(f"[Knowledge] Export failed: {e}")
            return jsonify({"success": False, "error": str(e)}), 500
