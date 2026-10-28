"""世界模拟 Web API

路由契约（前端已在用，路径/字段勿改名；扩展只加字段）：

- ``/api/worlds`` GET/POST，``/api/worlds/import`` POST，``/api/worlds/generate`` POST
  + ``/api/worlds/generate/<task_id>/status`` GET
- ``/api/worlds/<world_id>`` GET/PUT/DELETE，``/export`` GET，``/duplicate`` POST，``/tree`` GET
- ``/api/worlds/<world_id>/nodes/<node_id>`` GET/PUT/DELETE，``/activate`` ``/step``
  ``/step-stream`` ``/tick`` ``/jump-time`` ``/summarize``
- ``/api/worlds/<world_id>/events/<event_id>/branch`` POST
- ``/api/worlds/<world_id>/characters`` GET/POST，``/characters/import-card`` POST，
  ``/characters/<character_id>`` PUT/DELETE
- ``/api/worlds/<world_id>/assets`` GET/POST，``/assets/<asset_id>`` PUT/DELETE

旧版依赖 ``server.create_background_task`` 等辅助方法，那些方法随 server 一起丢了，
这里退化为模块内任务表（若 server 上有同名方法则优先用 server 的）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any, Dict, Optional

from flask import jsonify, request

from nbot.web.world_socket_events import emit_world_bundle_updated, emit_world_result
from nbot.world import CharacterResolver, WorldSimulation, WorldStore

_log = logging.getLogger(__name__)

_PROCESS_TASKS: Dict[str, Dict[str, Any]] = {}
_TASKS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _json_payload() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _error(message: Any, status: int = 400):
    text = str(message)
    return jsonify({"success": False, "error": text, "message": text}), status


def _not_found(message: Any):
    return _error(message, 404)


def _store_for(server: Any) -> WorldStore:
    store = getattr(server, "world_store", None)
    if isinstance(store, WorldStore):
        return store
    # 优先用 base_dir（仓库根，世界落 data/worlds）；测试里的假 server 只有 data_dir
    base_dir = getattr(server, "base_dir", None) or getattr(server, "data_dir", ".")
    store = WorldStore(base_dir)
    try:
        server.world_store = store
    except Exception:  # 只读对象时忽略
        pass
    return store


def _model_client(server: Any) -> Optional[Any]:
    if getattr(server, "ai_client", None) is None:
        return None
    responder = getattr(server, "_get_ai_response", None)
    if callable(responder):
        return responder
    client = getattr(server, "ai_client", None)
    return client if callable(client) else None


def _status_of(data: Dict[str, Any]) -> str:
    for key in ("summary", "name", "description"):
        if str(data.get(key) or "").strip():
            return str(data.get(key)).strip()
    return ""


def _task_table(server: Any) -> Dict[str, Dict[str, Any]]:
    """任务表挂在 server.world_tasks 上（与 portrait_generation_tasks 同风格）。

    这样任务是 server 的资产而不是模块全局的：多实例/测试隔离都干净，
    服务端也能列出或清理它们。server 不可写时才退回进程内共享表。
    """
    tasks = getattr(server, "world_tasks", None)
    if isinstance(tasks, dict):
        return tasks
    tasks = {}
    try:
        server.world_tasks = tasks
    except Exception:
        _log.warning("[WorldRoutes] server 不可写，世界任务表退化为进程内共享表")
        return _PROCESS_TASKS
    return tasks


def _create_task(
    server: Any, kind: str, task_id: str, meta: Optional[Dict[str, Any]] = None
) -> None:
    task = {
        "task_id": task_id,
        "kind": kind,
        "status": "pending",
        "progress": 0,
        "message": "排队中",
        "result": {},
        "error": "",
        **(meta or {}),
    }
    with _TASKS_LOCK:
        _task_table(server)[task_id] = task


def _update_task(server: Any, task_id: str, **fields: Any) -> None:
    with _TASKS_LOCK:
        task = _task_table(server).get(task_id)
        if task is None:
            return
        task.update(fields)


def _get_task(server: Any, task_id: str) -> Optional[Dict[str, Any]]:
    with _TASKS_LOCK:
        task = _task_table(server).get(task_id)
        return dict(task) if task else None


def _node_id_of(bundle: Dict[str, Any]) -> str:
    node = bundle.get("current_node") if isinstance(bundle, dict) else None
    return str((node or {}).get("node_id") or "")


def _int_arg(name: str) -> Optional[int]:
    """读取可选的非负整数 query 参数，缺失或非法返回 None。"""
    raw = request.args.get(name)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = int(text)
    except ValueError:
        return None
    return value if value >= 0 else None


# --------------------------------------------------------------------------- #
# 后台任务
# --------------------------------------------------------------------------- #


def _run_ai_generate_world_task(
    task_id: str, server: Any, store: WorldStore, data: Dict[str, Any]
) -> None:
    _update_task(server, task_id, status="running", progress=10, message="AI 正在生成世界…")
    try:
        simulation = WorldSimulation(store, model_client=_model_client(server))
        bundle = simulation.generate_world(data)
        world = bundle.get("world") or {}
        _update_task(
            server,
            task_id,
            status="completed",
            progress=100,
            message=f"世界「{world.get('name', '')}」生成完成",
            result={"world_id": world.get("world_id", ""), "world": world},
        )
        _log.info(
            "AI 生成世界完成: %s, world_id: %s", world.get("name", ""), world.get("world_id", "")
        )
    except Exception as exc:
        _log.error("AI 生成世界失败: %s", exc)
        _update_task(
            server, task_id, status="failed", progress=100, message="生成失败", error=str(exc)
        )


def _run_world_step_task(
    task_id: str,
    server: Any,
    store: WorldStore,
    world_id: str,
    node_id: str,
    content: str,
    target_character_id: str,
    action_type: str,
) -> None:
    from nbot.web.world_socket_events import world_room

    socketio = getattr(server, "socketio", None)
    room = world_room(world_id, node_id)
    if socketio is not None:
        socketio.emit(
            "world_stream_start",
            {"world_id": world_id, "node_id": node_id, "action": "step"},
            to=room,
        )
    _update_task(server, task_id, status="running", progress=20, message="正在推进世界…")
    try:
        simulation = WorldSimulation(store, model_client=_model_client(server))
        result = simulation.step(
            world_id,
            node_id,
            content,
            target_character_id=target_character_id,
            action_type=action_type,
        )
        payload = result.to_dict()
        if socketio is not None:
            socketio.emit(
                "world_stream_end",
                {"world_id": world_id, "node_id": node_id, "action": "step", "result": payload},
                to=room,
            )
        emit_world_result(server, result, "step")
        _update_task(
            server,
            task_id,
            status="completed",
            progress=100,
            message="推进完成",
            result=payload if isinstance(payload, dict) else {},
        )
        _log.info("World step task completed: %s", task_id)
    except Exception as exc:
        _log.error("World step task failed: %s, error: %s", task_id, exc)
        if socketio is not None:
            socketio.emit(
                "world_stream_end",
                {"world_id": world_id, "node_id": node_id, "action": "step", "error": str(exc)},
                to=room,
            )
        _update_task(
            server, task_id, status="failed", progress=100, message="推进失败", error=str(exc)
        )


# --------------------------------------------------------------------------- #
# 路由注册
# --------------------------------------------------------------------------- #


def register_world_routes(app, server) -> None:
    store = _store_for(server)

    # ---------------------------- 世界 ---------------------------- #

    @app.route("/api/worlds", methods=["GET"])
    def list_worlds():
        raw = str(request.args.get("include_archived", "1")).strip().lower()
        include_archived = raw not in ("0", "false", "no", "off", "")
        return jsonify({"success": True, "worlds": store.list_worlds(include_archived)})

    @app.route("/api/worlds", methods=["POST"])
    def create_world():
        try:
            bundle = store.create_world(_json_payload())
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception as exc:
            _log.error("Failed to create world: %s", exc)
            return _error("Failed to create world", 500)
        emit_world_bundle_updated(server, bundle["world"]["world_id"], _node_id_of(bundle), "created")
        return jsonify({"success": True, **bundle}), 201

    @app.route("/api/worlds/import", methods=["POST"])
    def import_world():
        try:
            bundle = store.import_world(_json_payload())
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception as exc:
            _log.error("Failed to import world: %s", exc)
            return _error("Failed to import world", 500)
        emit_world_bundle_updated(server, bundle["world"]["world_id"], _node_id_of(bundle), "imported")
        return jsonify({"success": True, **bundle}), 201

    @app.route("/api/worlds/generate", methods=["POST"])
    def generate_world():
        data = _json_payload()
        if not _status_of(data):
            return _error("世界描述不能为空", 400)
        if _model_client(server) is None:
            return _error("AI 模型未配置，无法生成世界", 400)

        task_id = f"ai-generate-world-{uuid.uuid4().hex[:8]}"
        _create_task(
            server, "ai_generate_world", task_id, {"world_name": _status_of(data)[:60]}
        )
        thread = threading.Thread(
            target=_run_ai_generate_world_task,
            args=(task_id, server, store, data),
            daemon=True,
        )
        thread.start()
        _log.info("AI 世界生成任务已提交: %s", task_id)
        return (
            jsonify(
                {
                    "success": True,
                    "task_id": task_id,
                    "message": "已开始生成，可通过任务状态接口查询进度",
                }
            ),
            202,
        )

    @app.route("/api/worlds/generate/<task_id>/status", methods=["GET"])
    def get_generate_world_status(task_id: str):
        task = _get_task(server, task_id)
        if task is None:
            return _not_found("任务不存在")
        return jsonify({"success": True, "task": task})

    @app.route("/api/worlds/<world_id>", methods=["GET"])
    def get_world(world_id: str):
        event_limit = _int_arg("event_limit")
        before_sequence = _int_arg("before_sequence")
        try:
            bundle = store.get_world_bundle(
                world_id, event_limit=event_limit, before_sequence=before_sequence
            )
        except KeyError:
            return _not_found("World not found")
        return jsonify({"success": True, **bundle})

    @app.route("/api/worlds/<world_id>", methods=["PUT"])
    def update_world(world_id: str):
        try:
            bundle = store.update_world(world_id, _json_payload())
            emit_world_bundle_updated(
                server, world_id, _node_id_of(bundle), "world_updated", {"world": bundle["world"]}
            )
        except KeyError:
            return _not_found("World not found")
        except ValueError as exc:
            return _error(str(exc), 400)
        return jsonify({"success": True, **bundle})

    @app.route("/api/worlds/<world_id>", methods=["DELETE"])
    def delete_world(world_id: str):
        try:
            bundle = store.archive_world(world_id)
        except KeyError:
            return _not_found("World not found")
        emit_world_bundle_updated(server, world_id, "", "world_archived")
        return jsonify({"success": True, **bundle})

    @app.route("/api/worlds/<world_id>/export", methods=["GET"])
    def export_world(world_id: str):
        try:
            export = store.export_world(world_id)
        except KeyError:
            return _not_found("World not found")
        return jsonify({"success": True, "export": export})

    @app.route("/api/worlds/<world_id>/duplicate", methods=["POST"])
    def duplicate_world(world_id: str):
        try:
            bundle = store.duplicate_world(world_id)
        except KeyError:
            return _not_found("World not found")
        emit_world_bundle_updated(
            server, bundle["world"]["world_id"], _node_id_of(bundle), "duplicated"
        )
        return jsonify({"success": True, **bundle}), 201

    @app.route("/api/worlds/<world_id>/tree", methods=["GET"])
    def get_world_tree(world_id: str):
        if store.get_world(world_id) is None:
            return _not_found("World not found")
        return jsonify({"success": True, "tree": store.get_tree(world_id)})

    # ---------------------------- 节点 ---------------------------- #

    @app.route("/api/worlds/<world_id>/nodes/<node_id>", methods=["GET", "PUT", "DELETE"])
    def get_world_node(world_id: str, node_id: str):
        if request.method == "GET":
            node = store.get_node(world_id, node_id)
            if not node:
                return _not_found("Node not found")
            return jsonify({"success": True, "node": node})

        if request.method == "PUT":
            try:
                node = store.update_node(world_id, node_id, _json_payload())
                emit_world_bundle_updated(
                    server, world_id, node_id, "node_updated", {"node": node}
                )
            except KeyError:
                return _not_found("Node not found")
            except ValueError as exc:
                return _error(str(exc), 400)
            return jsonify({"success": True, "node": node})

        try:
            bundle = store.delete_node(world_id, node_id)
            current_node_id = (bundle.get("current_node") or {}).get("node_id", "")
            emit_world_bundle_updated(
                server, world_id, current_node_id, "node_deleted", {"deleted_node_id": node_id}
            )
        except KeyError:
            return _not_found("Node not found")
        except ValueError as exc:
            return _error(str(exc), 400)
        return jsonify({"success": True, **bundle})

    @app.route("/api/worlds/<world_id>/nodes/<node_id>/activate", methods=["POST"])
    def activate_world_node(world_id: str, node_id: str):
        try:
            bundle = store.activate_node(world_id, node_id)
        except KeyError:
            return _not_found("Node not found")
        emit_world_bundle_updated(server, world_id, node_id, "node_activated")
        return jsonify({"success": True, **bundle})

    @app.route("/api/worlds/<world_id>/nodes/<node_id>/step", methods=["POST"])
    def world_step(world_id: str, node_id: str):
        data = _json_payload()
        content = str(data.get("content") or "").strip()
        if not content:
            return _error("content is required", 400)

        try:
            simulation = WorldSimulation(store, model_client=_model_client(server))
            result = simulation.step(
                world_id,
                node_id,
                content,
                target_character_id=str(data.get("target_character_id") or ""),
                action_type=str(data.get("action_type") or "act"),
            )
            emit_world_result(server, result, "step")
        except KeyError:
            return _not_found("World or node not found")
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception as exc:
            _log.error("World step failed: %s", exc)
            return _error("执行失败", 500)
        return jsonify({"success": True, **result.to_dict()})

    @app.route("/api/worlds/<world_id>/nodes/<node_id>/step-stream", methods=["POST"])
    def world_step_stream(world_id: str, node_id: str):
        data = _json_payload()
        content = str(data.get("content") or "").strip()
        if not content:
            return _error("content is required", 400)
        if _model_client(server) is None:
            return _error("AI 模型未配置", 400)

        task_id = f"world-step-{uuid.uuid4().hex[:8]}"
        _create_task(server, "world_step", task_id, {"world_id": world_id, "node_id": node_id})
        thread = threading.Thread(
            target=_run_world_step_task,
            args=(
                task_id,
                server,
                store,
                world_id,
                node_id,
                content,
                str(data.get("target_character_id") or ""),
                str(data.get("action_type") or "act"),
            ),
            daemon=True,
        )
        thread.start()
        _log.info("World step stream started: %s", task_id)
        return jsonify({"success": True, "task_id": task_id, "message": "已开始推进"}), 202

    @app.route("/api/worlds/<world_id>/nodes/<node_id>/tick", methods=["POST"])
    def world_tick(world_id: str, node_id: str):
        data = _json_payload()
        try:
            elapsed = int(data.get("elapsed_seconds", 60))
        except (TypeError, ValueError):
            elapsed = 60

        try:
            simulation = WorldSimulation(store, model_client=_model_client(server))
            result = simulation.tick(world_id, node_id, elapsed_seconds=elapsed)
            emit_world_result(server, result, "tick")
        except KeyError:
            return _not_found("World or node not found")
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception as exc:
            _log.error("World tick failed: %s", exc)
            return _error("时间推进失败", 500)
        return jsonify({"success": True, **result.to_dict()})

    @app.route("/api/worlds/<world_id>/nodes/<node_id>/jump-time", methods=["POST"])
    def world_jump_time(world_id: str, node_id: str):
        data = _json_payload()
        target_time = str(data.get("target_time") or data.get("time") or "").strip()
        if not target_time:
            return _error("target_time is required", 400)

        try:
            simulation = WorldSimulation(store, model_client=_model_client(server))
            result = simulation.jump_time(world_id, node_id, target_time)
            emit_world_result(server, result, "jump_time")
        except KeyError:
            return _not_found("World or node not found")
        except ValueError as exc:
            return _error(str(exc), 400)
        except Exception as exc:
            _log.error("World jump_time failed: %s", exc)
            return _error("时间跳转失败", 500)
        return jsonify({"success": True, **result.to_dict()})

    @app.route("/api/worlds/<world_id>/nodes/<node_id>/summarize", methods=["POST"])
    def summarize_world_node(world_id: str, node_id: str):
        try:
            node = store.summarize_node(world_id, node_id)
            client = _model_client(server)
            if client is not None:
                node = WorldSimulation(store, model_client=client).generate_node_summary(
                    world_id, node_id
                )
            emit_world_bundle_updated(
                server, world_id, node_id, "node_summarized", {"node": node}
            )
        except KeyError:
            return _not_found("Node not found")
        return jsonify({"success": True, "node": node})

    # ---------------------------- 分支 ---------------------------- #

    @app.route("/api/worlds/<world_id>/events/<event_id>/branch", methods=["POST"])
    def branch_world(world_id: str, event_id: str):
        data = _json_payload()
        branch_name = str(data.get("branch_name") or data.get("name") or "").strip()
        try:
            bundle = store.branch_from_event(world_id, event_id, branch_name)
        except KeyError:
            return _not_found("Event not found")
        emit_world_bundle_updated(
            server, world_id, _node_id_of(bundle), "branch_created", {"event_id": event_id}
        )
        return jsonify({"success": True, **bundle}), 201

    # ---------------------------- 角色 ---------------------------- #

    @app.route("/api/worlds/<world_id>/characters", methods=["GET"])
    def list_world_characters(world_id: str):
        if store.get_world(world_id) is None:
            return _not_found("World not found")
        return jsonify({"success": True, "characters": store.list_characters(world_id)})

    @app.route("/api/worlds/<world_id>/characters", methods=["POST"])
    def create_world_character(world_id: str):
        if store.get_world(world_id) is None:
            return _not_found("World not found")
        try:
            character = store.upsert_character(world_id, _json_payload())
        except ValueError as exc:
            return _error(str(exc), 400)
        bundle = store.get_world_bundle(world_id)
        emit_world_bundle_updated(
            server,
            world_id,
            _node_id_of(bundle),
            "character_upserted",
            {"character": character},
        )
        return jsonify({"success": True, "character": character}), 201

    @app.route("/api/worlds/<world_id>/characters/import-card", methods=["POST"])
    def import_world_character_card(world_id: str):
        if store.get_world(world_id) is None:
            return _not_found("World not found")

        data = _json_payload()
        resolver = CharacterResolver()
        source_type = str(data.get("source_type") or "imported")
        source_id = str(data.get("source_id") or "")
        try:
            # 酒馆 v1-v3 卡自动走 from_tavern_card（保留内嵌世界书/元信息），
            # 原生预设走 from_native_card，其余按别名归一化
            items = resolver.normalize_payload(
                data, source_type=source_type, source_id=source_id
            )
            if not items:
                return _error("角色卡解析失败", 400)
            characters = [store.upsert_character(world_id, item) for item in items]
        except ValueError as exc:
            return _error(str(exc), 400)

        bundle = store.get_world_bundle(world_id)
        emit_world_bundle_updated(
            server,
            world_id,
            _node_id_of(bundle),
            "characters_imported",
            {"characters": characters},
        )
        return jsonify({"success": True, "characters": characters}), 201

    @app.route(
        "/api/worlds/<world_id>/characters/<character_id>", methods=["PUT", "DELETE"]
    )
    def update_world_character(world_id: str, character_id: str):
        if request.method == "PUT":
            payload = _json_payload()
            payload["character_id"] = character_id
            try:
                character = store.upsert_character(world_id, payload)
            except ValueError as exc:
                return _error(str(exc), 400)
            bundle = store.get_world_bundle(world_id)
            emit_world_bundle_updated(
                server,
                world_id,
                _node_id_of(bundle),
                "character_upserted",
                {"character": character},
            )
            return jsonify({"success": True, "character": character})

        try:
            store.delete_character(world_id, character_id)
        except KeyError:
            return _not_found("Character not found")
        bundle = store.get_world_bundle(world_id)
        emit_world_bundle_updated(
            server,
            world_id,
            _node_id_of(bundle),
            "character_deleted",
            {"character_id": character_id},
        )
        return jsonify({"success": True, "character_id": character_id})

    # ---------------------------- 资产 ---------------------------- #

    @app.route("/api/worlds/<world_id>/assets", methods=["GET"])
    def list_world_assets(world_id: str):
        if store.get_world(world_id) is None:
            return _not_found("World not found")
        asset_type = str(request.args.get("type") or "").strip()
        return jsonify({"success": True, "assets": store.list_assets(world_id, asset_type)})

    @app.route("/api/worlds/<world_id>/assets", methods=["POST"])
    def create_world_asset(world_id: str):
        if store.get_world(world_id) is None:
            return _not_found("World not found")
        try:
            asset = store.upsert_asset(world_id, _json_payload())
        except ValueError as exc:
            return _error(str(exc), 400)
        bundle = store.get_world_bundle(world_id)
        emit_world_bundle_updated(
            server, world_id, _node_id_of(bundle), "asset_upserted", {"asset": asset}
        )
        return jsonify({"success": True, "asset": asset}), 201

    @app.route("/api/worlds/<world_id>/assets/<asset_id>", methods=["PUT", "DELETE"])
    def update_world_asset(world_id: str, asset_id: str):
        if request.method == "PUT":
            payload = _json_payload()
            payload["asset_id"] = asset_id
            try:
                asset = store.upsert_asset(world_id, payload)
            except ValueError as exc:
                return _error(str(exc), 400)
            bundle = store.get_world_bundle(world_id)
            emit_world_bundle_updated(
                server, world_id, _node_id_of(bundle), "asset_upserted", {"asset": asset}
            )
            return jsonify({"success": True, "asset": asset})

        try:
            store.delete_asset(world_id, asset_id)
        except KeyError:
            return _not_found("Asset not found")
        bundle = store.get_world_bundle(world_id)
        emit_world_bundle_updated(
            server, world_id, _node_id_of(bundle), "asset_deleted", {"asset_id": asset_id}
        )
        return jsonify({"success": True, "asset_id": asset_id})
