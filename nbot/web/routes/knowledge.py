import json
import logging

from flask import jsonify, request

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
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return jsonify(
                {"success": False, "error": "Knowledge manager not available"}
            ), 503
        return jsonify({"success": True, "message": "知识库索引会在后台自动处理"})

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
